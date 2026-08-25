"""Container state monitoring (#2, @NotRetarded).

Watches for state *transitions* between scheduler ticks and notifies:

- health went unhealthy (debounced: confirmed on the next pass before we
  alert, so flappy healthchecks don't spam) and the recovery back to healthy
- container exited with a non-zero code (zero exits are normal endings —
  one-shot jobs would spam otherwise)
- container was OOM-killed
- container crashed and was brought back by its restart policy
  (RestartCount increased since the last pass — status-agnostic, so an
  instant-crash loop stuck in restart-backoff is still caught)

Design constraints, in order of importance:

1. NO false alarms during updates: while any update flow holds the update
   mutex, containers legitimately stop/start — the whole tick is skipped.
2. Transitions only, never states: an unhealthy container fires once, not
   every tick. A per-(container, kind) cooldown (default 30 min) guards
   the edge where a state flaps.
3. Opt-outs at every level: MONITOR=false kills the feature,
   `docksentry.monitor=false` takes a single container out (label family,
   #42), exclude_containers is honored, and our own container is skipped
   (the update machinery reports on Docksentry itself).
4. First tick is a silent baseline — a restart of Docksentry must not
   re-announce the world.
"""
import json
import os
import subprocess
import time
from datetime import datetime


def _clock(iso):
    """`HH:MM:SS` in LOCAL time from Docker's RFC3339 timestamp.

    Only the time of day: the alert lands within a minute of the event, so
    a date adds noise, and a full RFC3339 string with nanoseconds is
    unreadable in a chat message. Anything unparseable degrades to empty
    rather than printing a raw timestamp at the user.

    Docker stamps StartedAt in UTC — the trailing Z — and this used to slice
    the time out and print it as-is. @NotRetarded saw a crash at 23:29 his
    time reported as `03:29:20`: exactly his UTC-4 offset (#2). Worse, the
    same alert carried two clocks in two zones, because the event log's own
    timestamp comes from datetime.now() and is local. Converted here, so
    every time Docksentry prints is the wall clock of the machine it runs
    on — set TZ on the container and both agree.
    """
    if not iso or not isinstance(iso, str):
        return ""
    # "2026-08-01T16:19:25.123456789Z" → local HH:MM:SS.
    try:
        head, _, rest = iso.partition("T")
        if not rest:
            return ""
        # Python cannot parse nanoseconds; trim the fraction to microseconds
        # and normalise the Z so fromisoformat accepts it on 3.10 too.
        body = rest.rstrip("Z")
        clock, dot, frac = body.partition(".")
        stamp = f"{head}T{clock}" + (f".{frac[:6]}" if dot else "") + "+00:00"
        return datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
    except (IndexError, AttributeError, ValueError):
        # An unexpected shape must not cost the whole alert its timestamp:
        # the raw UTC time of day is still better than nothing.
        try:
            return iso.split("T", 1)[1].split(".", 1)[0].rstrip("Z")[:8]
        except (IndexError, AttributeError):
            return ""



#: The event kinds that mean "a container stopped or crashed" — the ones a
#: host reboot / `docker restart` fires all at once, and therefore the ones
#: the mass-stop digest coalesces. Health flips are not here: a container
#: turning unhealthy is its own event and stays individual.
_DEATH_KINDS = ("exited", "oom", "crash_restart")


class ContainerMonitor:
    COOLDOWN_SECONDS = 1800

    #: Up to this many deaths in a single tick stay individual, full-detail
    #: alerts (log tail inline). Above it, a host reboot or daemon restart is
    #: the likely cause, not that many separate incidents — so they collapse
    #: to one host digest plus a log file (#63, @famewolf: a dozen crash
    #: dumps for one planned shutdown was "unsustainable"). Three is where a
    #: readable stack of alerts becomes a wall.
    MASS_STOP_INLINE_MAX = 3

    def __init__(self, config, checker, bot, backend=None, host_name=""):
        self.config = config
        self.checker = checker
        self.bot = bot
        # The container CLI seam (v2 groundwork). Defaulting to get_backend
        # here keeps existing construction sites (scheduler, tests) working
        # unchanged while routing the docker reads through the backend.
        if backend is None:
            from container_backend import get_backend
            backend = get_backend(config)
        self.backend = backend
        #: Empty for the local host, the host's name for a remote one. Only
        #: ever used to LABEL a message — never to key state, which each
        #: monitor keeps to itself. Local stays unlabelled so single-host
        #: installs read exactly as they always did.
        self.host_name = host_name
        # Live event stream. It never alerts — it only records the memory
        # picture at the *instant* of a death, which this poller then uses
        # instead of taking one up to 60 seconds too late (#2,
        # @NotRetarded). Started lazily on the first tick so construction
        # stays cheap and a disabled watcher costs nothing.
        self.watcher = None
        self._watcher_tried = False
        self._prev = None          # name -> state dict; None = no baseline yet
        self._last_sent = {}       # (name, kind) -> monotonic ts
        # Health debounce (#2, @famewolf): a healthy->unhealthy flip is held
        # for one pass before we alert, so flappy healthchecks (gluetun's
        # ICMP-mismatch blip) that self-resolve within a minute stay silent.
        self._health_pending = {}  # name -> pre-unhealthy health (awaiting confirm)
        self._alerted_unhealthy = set()  # names we've actually fired unhealthy for

    # ── docker reads ────────────────────────────────────────────

    def snapshot(self):
        """Current state of all containers (running and stopped), or None
        when docker can't be read (never diff against a broken snapshot)."""
        try:
            ps = self.backend.ps(all=True, fmt="{{.Names}}", timeout=30)
            if ps.returncode != 0:
                return None
            names = [n for n in ps.stdout.strip().split("\n") if n]
            if not names:
                return {}
            ins = self.backend.inspect(names, timeout=30)
            if ins.returncode != 0:
                return None
            data = json.loads(ins.stdout) or []
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return None

        snap = {}
        for cfg in data:
            name = (cfg.get("Name") or "").lstrip("/")
            state = cfg.get("State") or {}
            snap[name] = {
                "status": state.get("Status", ""),
                "health": (state.get("Health") or {}).get("Status", ""),
                "exit_code": state.get("ExitCode", 0),
                "oom": bool(state.get("OOMKilled")),
                "restarts": cfg.get("RestartCount", 0) or 0,
                # When the container last came up, per Docker — NOT when we
                # noticed. The monitor samples every 60s, so "now" would put
                # a timestamp on the alert that can be a minute off and
                # invites the reader to line it up with the wrong action.
                "started_at": state.get("StartedAt", "") or "",
                "labels": (cfg.get("Config") or {}).get("Labels") or {},
                # What the healthcheck itself printed when it last failed.
                # Free — this inspect already ran — and it is the one thing
                # that says WHY a container turned unhealthy. `docker logs`
                # does not: measured on a container whose probe printed
                # "connection refused: upstream 10.0.0.5:8080", the log was
                # completely empty (#2, @famewolf: "Normally there is a
                # last log when a container goes unhealthy?").
                "health_output": self._health_output(state),
            }
        return snap

    @staticmethod
    def _health_output(state):
        """Output of the most recent FAILING healthcheck probe, trimmed.

        Falls back to the last probe of any kind, because a container that
        has just recovered still explains itself through what it said while
        it was down. Empty when the container has no healthcheck, or when
        the probe printed nothing.
        """
        log = ((state.get("Health") or {}).get("Log") or [])
        if not log:
            return ""
        failing = [e for e in log if e.get("ExitCode")]
        entry = (failing or log)[-1]
        out = (entry.get("Output") or "").strip()
        # Probes are usually a line or two; a curl that dumps a whole error
        # page is the case worth capping. Kept well under the log tail's
        # 1500 so both together stay inside Telegram's message limit.
        return out[:500] if len(out) <= 500 else out[:500] + "…"

    # ── pure logic ──────────────────────────────────────────────

    def _monitored(self, name, info):
        if name in (self.config.exclude_containers or []):
            return False
        try:
            if self.checker.label_bool(info.get("labels"), "monitor") is False:
                return False
        except Exception:
            pass
        return True

    @staticmethod
    def diff(prev, cur):
        """Transition events between two snapshots — exits, OOM kills and
        crash-restarts only. Pure; returns a list of (kind, name, detail)
        tuples. These stay IMMEDIATE (a death is a death). Health
        transitions are debounced separately, see _health_events.
        Containers that vanished are NOT events (removal is deliberate);
        new containers just get baselined.
        """
        events = []
        for name, now in cur.items():
            before = prev.get(name)
            if before is None:
                continue

            # Crash loop: RestartCount climbed since last pass, so the restart
            # POLICY fired — the container crashed and was auto-restarted. This
            # is the loop signal and it is STATUS-AGNOSTIC on purpose. A
            # container that crashes instantly on startup spends almost all its
            # time in restart-backoff (Docker reports status "restarting",
            # Podman may report "exited" between attempts), so at a 60s sample
            # it is rarely caught "running" — keying the old detector on
            # status == "running" missed the loop entirely (#2, @NotRetarded's
            # VPN-on-unsupported-kernel case, no healthcheck). Keying off the
            # count instead catches it whether we sample it running, restarting
            # or briefly exited, on both Docker and Podman. RestartCount only
            # increments on a POLICY restart — a manual `docker restart` does
            # NOT bump it — so a climbing count is always a real crash loop
            # worth one alert; the 30-min per-(container,kind) cooldown already
            # prevents spam. A recreate resets the counter to 0, so only a real
            # increase fires. (OOM keeps precedence over crash_restart.)
            if now["restarts"] > before["restarts"]:
                if now["oom"]:
                    events.append(("oom", name, {"code": now["exit_code"]}))
                else:
                    # The exit code and the restart time are what let the
                    # reader tell "this was me" from "this happened on its
                    # own". Without them a crash-restart alert that arrives
                    # while someone is shutting containers down looks like a
                    # false positive, and they go hunting for a bug in the
                    # detector instead of in their stack (#2, @famewolf).
                    # `exit_code` is the PREVIOUS run's — the one that died —
                    # because the container is up again by the time we look.
                    events.append(("crash_restart", name,
                                   {"count": now["restarts"],
                                    "code": before.get("exit_code", 0),
                                    "when": _clock(now.get("started_at"))}))
            # One-time death, count UNCHANGED: running -> exited. A container
            # that crashes then STAYS down (no restart policy, or the policy
            # gave up) lands here on the pass its status settles to exited with
            # the count no longer moving. `elif` so a looping container fires
            # crash_restart only, never a duplicate "exited" on the same pass.
            elif before["status"] == "running" and now["status"] == "exited":
                if now["oom"]:
                    events.append(("oom", name, {"code": now["exit_code"]}))
                elif now["exit_code"] != 0:
                    events.append(("exited", name, {"code": now["exit_code"]}))
                # zero exit: a normal ending, stay quiet
        return events

    @staticmethod
    def _health_events(prev, cur, pending, alerted):
        """Debounced health transitions (#2, @famewolf). Pure.

        A healthy->unhealthy flip does NOT alert on its own — it becomes
        *pending*. Only when the container is STILL unhealthy on the NEXT
        pass do we fire the unhealthy alert and remember we did. A recovery
        fires only if we actually alerted the unhealthy for it; a blip that
        resolves before confirmation produces ZERO notifications — neither
        unhealthy nor recovered.

        `pending` maps a name to the health it had right before it went
        unhealthy (so the alert can still say "health was: healthy").
        `alerted` is the set of names whose unhealthy we've fired. Returns
        (events, new_pending, new_alerted); the caller stores the two back.
        """
        events = []
        new_pending = dict(pending)
        new_alerted = set(alerted)
        for name, now in cur.items():
            before = prev.get(name)
            if before is None:
                continue
            # Health only means something for a RUNNING container. A stopped/
            # exited container's State.Health.Status is a stale frozen value
            # from when it last ran, not a live signal (#2, @famewolf: a
            # months-dead container still read "unhealthy" and fired a bogus
            # alert). Its stop/exit is already reported by diff(), so don't
            # double-report it here — and clear any flags so it starts clean if
            # it ever runs again. Stopping is NOT recovering: a previously
            # alerted container that merely stops gets no "recovered" event,
            # just a silent flag cleanup.
            if now.get("status") != "running":
                new_pending.pop(name, None)
                new_alerted.discard(name)
                continue
            h_now = now["health"]
            h_before = before["health"]

            if h_now == "unhealthy":
                if name in new_alerted:
                    continue                      # already alerted, stay quiet
                if name in new_pending:
                    # we saw it flip healthy->unhealthy last pass and it's still
                    # unhealthy -> confirmed, alert now with the pre-flip state
                    origin = new_pending.pop(name)
                    events.append(("unhealthy", name, {"prev": origin or "?"}))
                    new_alerted.add(name)
                elif h_before != "unhealthy":
                    # fresh healthy->unhealthy flip -> pend; remember pre-state
                    new_pending[name] = h_before or "?"
                # else: already unhealthy at baseline / never observed flipping
                # -> stay silent (silent-baseline-on-restart principle). "prev"
                # can therefore never be "unhealthy" in an emitted event.
            elif h_before == "unhealthy":
                # The episode ends the moment the container LEAVES unhealthy,
                # no matter where it lands. Recovery-by-restart goes
                # unhealthy->starting->healthy and never touches "healthy"
                # directly, so keying recovery on ->healthy stranded the name
                # in `alerted` forever and muted it for good. Leaving unhealthy
                # IS the recovery signal.
                new_pending.pop(name, None)
                if name in new_alerted:
                    events.append(("recovered", name, {}))
                    new_alerted.discard(name)
        # A vanished (or recreated) container must not drag a stale flag into
        # its next life — prune anything no longer present so it starts clean.
        new_pending = {n: v for n, v in new_pending.items() if n in cur}
        new_alerted = {n for n in new_alerted if n in cur}
        return events, new_pending, new_alerted

    # ── tick ────────────────────────────────────────────────────

    def _with_real_exit_code(self, event):
        """Replace a crash-restart's exit code with the one the event
        stream saw.

        `docker inspect` cannot supply it. A container that crashed and was
        restarted by its policy is *running* again by the time the poller
        looks, and a running container reports `ExitCode: 0` — measured, and
        true of the previous sweep as well, since it was running then too.
        So every crash-restart alert has been quoting a 0 that means
        "no information", right where a reader needs the number most: it is
        what distinguishes "this happened on its own" from "this was my own
        `docker stop`" (#2, @famewolf).

        The `die` event carries the real code. Where there is no watcher the
        old value stands, which is no worse than before.
        """
        kind, name, detail = event
        if kind != "crash_restart":
            return event
        watcher = getattr(self, "watcher", None)
        if not watcher:
            return event
        code = watcher.exit_code(name)
        if code is None:
            return event
        detail = dict(detail)
        detail["code"] = code
        return kind, name, detail

    def _ensure_watcher(self):
        """Start the event stream once, on the first tick.

        Lazy rather than in `__init__` because the constructor runs in the
        scheduler's start-up path, and a thread spawned there would be
        harder to reason about than one started by the loop that uses it.
        Tried exactly once: a runtime whose CLI has no `events` subcommand
        should cost one failed attempt, not one per minute forever.
        """
        if getattr(self, "_watcher_tried", False) or getattr(self, "watcher", None):
            return
        self._watcher_tried = True
        if not getattr(self.config, "monitor_events_enabled", True):
            return
        try:
            from event_watcher import EventWatcher
            w = EventWatcher(self.backend, self._event_snapshot,
                             log=lambda m: print(m))
            w.start()
            self.watcher = w
        except Exception as e:
            # The poller alone is exactly the pre-v1.65 behaviour, so a
            # watcher that cannot start degrades to "as before" rather
            # than to "broken".
            print(f"Event watcher unavailable, polling only: {e}")

    def tick(self):
        """One monitoring pass. Returns the list of notified events (for
        tests); [] when skipped or quiet."""
        if not getattr(self.config, "monitor_enabled", True):
            return []
        self._ensure_watcher()
        # Containers bounce legitimately while updates run — skip, and
        # also drop the baseline: diffing across an update window would
        # read every recreate as a crash.
        if getattr(self.bot, "update_running", False):
            self._prev = None
            # Baseline reset -> the health debounce resets with it, so a
            # stale "already alerted" entry can't swallow a real alert after
            # the update window (and pending flips don't survive it either).
            self._health_pending = {}
            self._alerted_unhealthy = set()
            return []

        cur = self.snapshot()
        if cur is None:
            return []
        own = None
        try:
            own = self.checker._own_container_name()
        except Exception:
            pass
        if own:
            cur.pop(own, None)

        if self._prev is None:
            self._prev = cur          # silent baseline
            return []

        health_events, self._health_pending, self._alerted_unhealthy = \
            self._health_events(self._prev, cur, self._health_pending,
                                self._alerted_unhealthy)
        raw = list(self.diff(self._prev, cur)) + health_events
        events = [
            (kind, name, detail)
            for kind, name, detail in raw
            if self._monitored(name, cur.get(name) or {})
        ]
        events = [self._with_real_exit_code(e) for e in events]
        # The host's running count BEFORE this tick's snapshot replaces it —
        # the denominator for "12 of 12 stopped". Read here, because the very
        # next lines overwrite `self._prev`.
        prev_running = sum(1 for i in self._prev.values()
                           if i.get("status") == "running")
        # `_prev` becomes `cur` here, so by the time _notify runs it is no
        # longer "previous" at all. Naming it separately rather than
        # relying on that: a reader who sees `self._prev` in _notify and
        # assumes it is the older snapshot would be wrong in a way nothing
        # would catch.
        self._latest = cur
        self._prev = cur

        sent = []
        now_ts = time.monotonic()
        # One `stats` call per tick at most. A host that loses twenty
        # containers at once produces twenty events in this loop, and the
        # snapshot costs ~2s each — serialised, that would delay the very
        # alerts it is decorating by the better part of a minute. All those
        # events happened in the same sweep anyway, so they share one
        # picture. Cleared here rather than kept, so the next tick measures
        # afresh instead of reporting a stale snapshot.
        self._snap_cache = None

        # Deaths are coalesced; health flips are not (a container turning
        # unhealthy is its own event). One host reboot or `docker restart`
        # stops every container at once — the old loop sent one alert, with
        # a full log dump, for EACH, and that flood is what drove @famewolf
        # off multi-host (#63). Up to MASS_STOP_INLINE_MAX deaths in a tick
        # stay individual, full-detail alerts (two unrelated crashes in a
        # minute are two real incidents); above it they are one host digest
        # plus a log file, because that many at once is a host going away,
        # not that many separate incidents.
        deaths = [(k, n, d) for k, n, d in events if k in _DEATH_KINDS]
        others = [(k, n, d) for k, n, d in events if k not in _DEATH_KINDS]

        # Per-(name,kind) cooldown still gates every death, so a container
        # crash-looping tick after tick is not re-reported every 60 s.
        fresh = []
        for k, n, d in deaths:
            if now_ts - self._last_sent.get((n, k), 0) < self.COOLDOWN_SECONDS:
                continue
            fresh.append((k, n, d))

        mass = (getattr(self.config, "monitor_mass_stop_enabled", True)
                and len(fresh) > self.MASS_STOP_INLINE_MAX)
        if fresh and mass:
            # The audit trail keeps every death individually — the digest
            # collapses the NOTIFICATION, never the record.
            for k, n, d in fresh:
                self._record(k, self._label(n), d, self._resources_for(k, n))
            self._record("mass_stop",
                         getattr(self, "host_name", "") or "local",
                         {"count": len(fresh), "total": prev_running}, {})
            hkey = ("__host__", "mass_stop")
            if now_ts - self._last_sent.get(hkey, 0) >= self.COOLDOWN_SECONDS:
                self._last_sent[hkey] = now_ts
                self._notify_mass_stop(fresh, prev_running)
            # A split reboot's second wave is recorded above but the host
            # cooldown holds back a duplicate host message. The per-container
            # cooldown is deliberately NOT stamped here: a container that
            # genuinely crashes minutes after the host settles must still
            # alert on its own.
            sent.extend(fresh)
        else:
            # Single crash or a small burst: unchanged behaviour — each one
            # its own full-detail alert with its log tail inline.
            for kind, name, detail in fresh:
                self._last_sent[(name, kind)] = now_ts
                resources = self._resources_for(kind, name)
                self._record(kind, self._label(name), detail, resources)
                self._notify(kind, name, detail, resources)
                sent.append((kind, name, detail))

        for kind, name, detail in others:
            key = (name, kind)
            if now_ts - self._last_sent.get(key, 0) < self.COOLDOWN_SECONDS:
                continue
            self._last_sent[key] = now_ts
            # Gathered once, before the write, and handed to both. It used
            # to be collected inside _notify — after the event had already
            # been recorded — so the log could never carry it. Doing it
            # here also means the alert and the history row cannot disagree
            # about what the machine looked like.
            resources = self._resources_for(kind, name)
            self._record(kind, self._label(name), detail, resources)
            self._notify(kind, name, detail, resources)
            sent.append((kind, name, detail))
        return sent

    MAX_EVENTS = 200

    def _record(self, kind, name, detail, resources=None):
        """Append to the persistent event log (shown in the Web UI's
        Events section). Telegram scrollback is no audit trail, and on
        headless installs without notification channels this file is the
        only place "what happened last night?" can be answered at all.
        `resources` is the memory/CPU picture at the moment of death. It
        rides along as its own key rather than inside `detail`, because
        `detail` is what fills the message template and must keep meaning
        exactly that. Absent on kinds where it says nothing, and absent on
        every event written before v1.75.0 — the renderer treats missing
        and empty the same way.

        Best-effort — a failed write must never break monitoring."""
        try:
            from datetime import datetime
            from container_store import atomic_write_json
            path = getattr(self.config, "monitor_events_file", None)
            if not path:
                return
            events = []
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        events = json.load(f) or []
                except (ValueError, OSError):
                    events = []
            entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "container": name,
                "detail": detail,
            }
            if resources:
                entry["resources"] = resources
            events.append(entry)
            atomic_write_json(path, events[-self.MAX_EVENTS:])
        except Exception as e:
            print(f"Monitor event log error: {e}")

    @staticmethod
    def _mem_to_bytes(s):
        """'1.5GiB' / '412MiB' / '820kB' from `docker stats` → bytes (0 on
        anything unparseable)."""
        s = (s or "").strip()
        units = {"KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
                 "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "B": 1}
        for u in sorted(units, key=len, reverse=True):
            if s.upper().endswith(u):
                try:
                    return float(s[:-len(u)].strip()) * units[u]
                except ValueError:
                    return 0
        return 0

    @staticmethod
    def host_memory_parts():
        """`(used_gb, total_gb, percent)` for this machine, or None.

        The numeric form behind `_host_memory`, for the Web UI, which needs
        a percentage to colour a card rather than a sentence. Both read the
        same `/proc/meminfo`, so the page and the alert can never disagree
        about how full the machine is.
        """
        vals = ContainerMonitor._meminfo()
        total = vals.get("MemTotal", 0)
        if not total:
            return None
        avail = vals.get("MemAvailable", vals.get("MemFree", 0))
        gb = 1024 ** 3
        used = total - avail
        return used / gb, total / gb, used / total * 100

    @staticmethod
    def _meminfo():
        """`/proc/meminfo` as `{key: bytes}`, or `{}` if unreadable.

        Inside a container this still reports the HOST's figures — the
        kernel namespaces processes, not memory accounting — which is
        exactly what both callers want.
        """
        vals = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    num = rest.strip().split(" ")[0]
                    if num.isdigit():
                        vals[key] = int(num) * 1024      # kB → bytes
        except OSError:
            return {}
        return vals

    def _host_memory(self):
        """This machine's memory as `6.2/11.4 GB · Swap 3.1/4.0 GB` — or
        "" if unreadable or if this monitor is watching a REMOTE host.

        That last condition is the important one. `/proc/meminfo` describes
        the machine Docksentry is running on; a monitor bound to a remote
        endpoint would happily print the local box's figures under a remote
        container's name and send someone hunting on the wrong machine.
        Wrong numbers are worse than none, so remote monitors get none.
        (Locality has bitten this project three times — HostScopedStore,
        default_backend(), the platform cache — so it is checked here
        rather than assumed from "the monitor is local today".)

        This answers the question that comes *before* "which container was
        the culprit": was the machine under memory pressure at all? Without
        it, a list of the biggest consumers invites the reader to blame
        whoever is at the top, even when there were 8 GB free and memory had
        nothing to do with it (#2, @NotRetarded).

        `/proc/meminfo` inside a container reports the HOST's figures — the
        kernel namespaces processes, not memory accounting — which is
        exactly what we want here. Costs a file read, so unlike the stats
        snapshot it is free.
        """
        backend = getattr(self, "backend", None)
        if backend is None or getattr(backend, "endpoint", None):
            # Remote, or locality unknown. Either way: say nothing. Wrong
            # numbers here are worse than none.
            return ""
        vals = self._meminfo()
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable", vals.get("MemFree", 0))
        if not total:
            return ""
        used = total - avail
        gb = 1024 ** 3
        # No words in the value — a live test showed "6.2/11.4 GB used"
        # sitting inside an otherwise German sentence. Everything
        # translatable belongs in the key; the value stays numbers plus
        # units, and used/total is carried by the slash.
        out = f"{used / gb:.1f}/{total / gb:.1f} GB"
        swap_total = vals.get("SwapTotal", 0)
        if swap_total:
            swap_used = swap_total - vals.get("SwapFree", 0)
            out += f" · Swap {swap_used / gb:.1f}/{swap_total / gb:.1f} GB"
        return out

    def _host_load(self):
        """This machine's run queue as `2.9 / 4 cores` — or "" when remote.

        The CPU half of `_host_memory`, and it was missing. `docker stats`
        reports CONTAINERS, so anything else burning the processor is
        invisible to it: an image pull, a compose build, a backup job, a
        host process. @NotRetarded's Unifi container died with exit 137
        during a `docker compose install` — memory nowhere near full (4.4
        of 7.6 GB, largest container 253 MiB) and no CPU line at all,
        because the load was the daemon unpacking layers rather than a
        container. The one reading that would have explained it was the one
        we never took.

        Same locality rule as the memory line: `/proc/loadavg` describes
        the machine Docksentry runs on, so a monitor bound to a remote
        endpoint stays silent rather than reporting the wrong box.

        Free — a file read, not a `docker stats` call.
        """
        backend = getattr(self, "backend", None)
        if backend is None or getattr(backend, "endpoint", None):
            return ""
        try:
            with open("/proc/loadavg") as f:
                one = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return ""
        cores = os.cpu_count() or 0
        # Numbers and units only; the words live in the translation key.
        return f"{one:.2f} / {cores}" if cores else f"{one:.2f}"

    def _stats_rows(self):
        """One `docker stats` at event time → `[(mem_bytes, cpu_pct, name,
        mem_str)]`, sorted by memory.

        Both readings come from the same call because they answer two
        halves of the same question, and the two mechanisms are otherwise
        indistinguishable from the outside. Measured 2026-08-01: a
        container starved to 0.5% of a core failed to answer SIGTERM
        inside its grace period and was SIGKILLed — **exit 137,
        OOMKilled false**, byte-identical to a kernel OOM kill. The same
        container on a full CPU exited 0. So an alert that reports only
        memory can point at the wrong resource entirely (#2,
        @NotRetarded, who saw CPU spikes and no memory pressure).

        Shared across a tick; see `_snap_cache`.
        """
        rows = getattr(self, "_snap_cache", None)
        if rows is not None:
            return rows
        rows = []
        backend = getattr(self, "backend", None)
        if backend is None:
            self._snap_cache = rows
            return rows
        try:
            r = backend.stats(
                fmt="{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}", timeout=30)
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    cname, usage, cpu = parts[0], parts[1], parts[2]
                    used = usage.split("/")[0].strip()
                    try:
                        pct = float(cpu.strip().rstrip("%"))
                    except ValueError:
                        pct = 0.0
                    rows.append((self._mem_to_bytes(used), pct,
                                 cname.strip(), used))
                rows.sort(reverse=True)
        except (subprocess.SubprocessError, OSError):
            rows = []
        # Cached even when empty: a daemon that just refused one stats
        # call will refuse the next nineteen too, and a burst of events
        # should not turn into a burst of doomed subprocesses.
        self._snap_cache = rows
        return rows

    def _memory_snapshot(self, top=3):
        """Top memory consumers at the moment of the event.

        Attached to any death — OOM, crash-restart, plain non-zero exit —
        because "which container did this to me?" is the same question in
        all three, and a container squeezed out by a neighbour does not
        necessarily get killed by the kernel's OOM killer (#2,
        @NotRetarded: his Unifi container died from a neighbour without an
        OOM flag, so the snapshot that could have named the culprit never
        fired).
        """
        rows = self._stats_rows()
        return " · ".join(f"{name} {used}" for _, _, name, used in rows[:top])

    #: A container has to be using at least half a core before it is worth
    #: naming. Below that the line is noise on an idle box — and the whole
    #: point of the CPU reading is contention, which does not exist at 3%.
    CPU_FLOOR = 50.0

    #: And below this, a container is not worth *listing* even once the
    #: line has been earned. Without it the line reads "hog 198% · gitlab
    #: 1% · redis 0%", where two thirds of it is filler that dilutes the
    #: one name that matters.
    CPU_LIST_FLOOR = 5.0

    def _cpu_snapshot(self, top=3):
        """Top CPU consumers at the moment of the event, or "".

        Reported only when something is actually eating CPU, because the
        failure mode this catches is *contention*: a container starved of
        CPU misses its SIGTERM window and gets SIGKILLed with exit 137,
        which reads exactly like an out-of-memory kill. Printing a CPU
        line on an idle host would bury that signal in noise.
        """
        rows = sorted(self._stats_rows(), key=lambda r: r[1], reverse=True)
        if not rows or rows[0][1] < self.CPU_FLOOR:
            return ""
        return " · ".join(f"{name} {pct:.0f}%"
                          for _, pct, name, _ in rows[:top]
                          if pct >= self.CPU_LIST_FLOOR)

    def _label(self, name):
        """How this container is named in a message.

        `nas/nginx` on a remote host, plain `nginx` locally. Without the
        prefix a fleet running the same stack on two machines produces two
        identical alerts and no way to tell which box is on fire (#7).

        Delegates to `container_store.host_key` rather than formatting the
        prefix here. That function already owns the rule that the local
        host stays unprefixed — a second copy of it is how the same
        container comes to be called two different things depending on
        which subsystem is talking.
        """
        host = getattr(self, "host_name", "")
        if not host:
            return name
        from container_store import host_key
        return host_key(host, name)

    def _event_snapshot(self):
        """Both readings as one opaque record, for the event watcher.

        The watcher stores whatever this returns without looking inside —
        it is the monitor that knows how to render it. That keeps the
        watcher free of message formatting, which is the poller's job.
        """
        return {"mem": self._memory_snapshot(), "cpu": self._cpu_snapshot()}

    def _resources_for(self, kind, name):
        """Who was holding memory and CPU at the moment this container died.

        Split out of `_notify` so the *stored* event can carry it too. It
        used to be gathered while building the message and then thrown away
        with it, which left the event log — the one place that exists to
        answer "what happened last night?" — unable to name the culprit
        (#2, @NotRetarded). His words: the alert is how he sees "in real
        time what CPU and Memory usage containers are using", and the
        history could not tell him afterwards.

        Returns {} for kinds where it means nothing (a recovery, a health
        flip), so callers can treat an empty result as "nothing to show"
        rather than "not measured".
        """
        if kind not in ("oom", "crash_restart", "exited"):
            return {}
        out = {}
        host_mem = self._host_memory()
        if host_mem:
            out["host"] = host_mem
        # Always, not gated on a floor: the question it answers is "was the
        # machine busy at all", and a low number is as much of an answer as
        # a high one. Gating it would repeat the mistake the top-CPU line
        # made — silence exactly when the load is not a container's.
        load = self._host_load()
        if load:
            out["load"] = load
        watcher = getattr(self, "watcher", None)
        # Asked BEFORE evidence(), which consumes the record.
        #
        # Three-valued on purpose (#2, @NotRetarded: "is there a way to add
        # the true or false flag on the code exit"). Exit 137 with the OOM
        # flag set means memory; without it, something else — and that is
        # the single most useful bit in the whole alert. But a bare "false"
        # that actually means "we never looked" would send him hunting in
        # the wrong direction, so it is only stated when the event stream
        # was there to see it. `inspect` is not consulted for this: it
        # reports the CURRENT run of a restarted container, and it was
        # measured false on rootless Podman for a container the kernel
        # really did kill.
        if watcher is not None and watcher.exit_code(name) is not None:
            out["oom_flag"] = "yes" if watcher.saw_oom(name) else "no"

        # What the container that DIED was using. The top-N list often does
        # not contain it — it released everything on the way out — so a
        # reader who knows their container is normally the biggest consumer
        # sees a list without it and cannot tell whether it had grown or
        # vanished (#2, @NotRetarded, whose Unifi normally sits at 1.5-1.7
        # GB and appeared nowhere). Absent when the container was already
        # gone by the time stats ran, which is itself the answer.
        own = self._own_usage(name)
        if own:
            out["victim"] = own

        # Evidence from the event stream first: it was taken at the moment
        # of death, while the culprit still held the memory. Falling back
        # to a fresh snapshot keeps the old behaviour wherever the watcher
        # is off, unavailable, or too late.
        rec = watcher.evidence(name) if watcher else ""
        if not rec:
            rec = self._event_snapshot()
        for k in ("mem", "cpu"):
            if rec.get(k):
                out[k] = rec[k]
        return out

    def _own_usage(self, name):
        """`1.6GiB · 12%` for one container, or "" when it is not in stats.

        Reads the shared per-tick snapshot, so it costs nothing beyond the
        `docker stats` the memory list already pays for. The name arrives
        unprefixed here — the host label is added later, for display only.
        """
        short = name.split("/", 1)[-1]
        for _, cpu, row_name, used in self._stats_rows():
            if row_name == short:
                return f"{used} · {cpu:.0f}%"
        return ""

    def _notify_mass_stop(self, deaths, prev_running):
        """One message for a whole host's worth of deaths in a tick, plus a
        log file carrying every container's tail.

        Why a file and not inline text: a host reboot stops a dozen
        containers, and a dozen inline log dumps is exactly the flood being
        removed (#63). The file keeps every line — more of them than the
        inline alert ever showed — and it is captured NOW, at detection,
        because a host that stays down can no longer be asked. Same reason
        the writable layer is measured before the recreate: once the thing
        is gone, so is its evidence. Best-effort: a container whose logs we
        cannot fetch is named in the file as unavailable rather than
        silently dropped.
        """
        t = self.bot.t
        host = getattr(self, "host_name", "") or "local"
        n = len(deaths)
        names = [self._label(nm).split("/")[-1] for _, nm, _ in deaths]
        # Honest about scale: a majority of the host going at once reads as
        # the host itself; a handful reads as a cluster, and claiming "the
        # host went down" over five of fifty would be a fabrication.
        if prev_running and n >= 0.5 * prev_running:
            head = t("monitor_mass_host_down", host=host, count=n,
                     total=prev_running)
        else:
            head = t("monitor_mass_cluster", host=host, count=n)
        msg = head + "\n" + ", ".join(names[:30]) + "\n" \
            + t("monitor_mass_footer")
        try:
            self.bot.send_message(msg, auto=True)
        except Exception as e:
            print(f"Monitor mass notify error: {e}")
        notifier = getattr(self.bot, "notifier", None)
        if notifier and getattr(notifier, "has_channels", lambda: False)():
            try:
                notifier.send_message(msg)
            except Exception as e:
                print(f"Monitor mass notifier error: {e}")
        # The log file — best-effort, only where the front end takes one.
        try:
            body = self._mass_log_file(deaths)
            send_doc = getattr(self.bot, "send_document", None)
            if body and send_doc:
                from datetime import datetime
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                fname = f"{host}-logs-{stamp}.txt".replace("/", "_")
                send_doc(fname, body.encode("utf-8"),
                         t("monitor_mass_file_caption", host=host))
        except Exception as e:
            print(f"Monitor mass log-file error: {e}")
        print(f"Monitor: {msg}")

    def _mass_log_file(self, deaths):
        """Every stopped container's log tail, one section each, as the text
        of a single attachment. The tails are longer than the inline alert's
        ten lines — a file has room the chat message never did."""
        out = []
        for kind, name, detail in deaths:
            code = detail.get("code", "?")
            out.append(f"===== {self._label(name)} (exit {code}) =====")
            try:
                # none_on_error tells a failed fetch (None) from a silent
                # container ("") — reporting a container that simply logged
                # nothing as "host unreachable" is a false alarm (#63).
                tail = self.checker._tail_logs(name, lines=40, none_on_error=True)
            except Exception:
                tail = None
            if tail is None:
                out.append("(logs unavailable — host unreachable, or the "
                           "container is gone)")
            elif not tail.strip():
                out.append("(container ran but produced no log output)")
            else:
                out.append(tail.strip())
            out.append("")
        return "\n".join(out)

    def _notify(self, kind, name, detail, resources=None):
        t = self.bot.t
        # `name` stays raw for the watcher lookup below — the event stream
        # reports the container's own name, unaware of which registry entry
        # it belongs to. Only the text the reader sees carries the host.
        shown = self._label(name)
        if kind == "unhealthy":
            msg = t("monitor_unhealthy", name=shown, prev=detail.get("prev", "?"))
        elif kind == "recovered":
            msg = t("monitor_recovered", name=shown)
        elif kind == "oom":
            msg = t("monitor_oom", name=shown, code=detail.get("code", "?"))
        elif kind == "crash_restart":
            # `code`/`when` are extra format kwargs: a translation that
            # hasn't picked them up yet simply ignores them, so no language
            # can break on this.
            msg = t("monitor_crash_restart", name=shown,
                    count=detail.get("count", "?"),
                    code=detail.get("code", "?"),
                    when=detail.get("when", "") or "?")
        else:
            msg = t("monitor_exited", name=shown, code=detail.get("code", "?"))

        # Name the culprit, not just the victim. This used to fire for OOM
        # only, which missed the commonest shape of the problem: a
        # container squeezed out by a neighbour often dies without the
        # kernel OOM-killing it, so the one alert that could have pointed
        # at the neighbour stayed silent.
        if resources is None:
            resources = self._resources_for(kind, name)
        if resources.get("host"):
            msg += "\n" + t("monitor_host_memory", state=resources["host"])
        if resources.get("load"):
            msg += "\n" + t("monitor_host_cpu", state=resources["load"])
        # The victim's own line goes ABOVE the top-N list: the reader's
        # first question is what the container that died was doing, and a
        # list of its neighbours answers a different one.
        if resources.get("victim"):
            msg += "\n" + t("monitor_victim_usage", name=shown,
                             state=resources["victim"])
        if resources.get("oom_flag"):
            msg += "\n" + t("monitor_oom_flag_" + resources["oom_flag"])
        if resources.get("mem"):
            msg += "\n" + t("monitor_top_memory", list=resources["mem"])
        # CPU only when something is actually contending for it. A
        # container starved of CPU misses its SIGTERM window and is
        # SIGKILLed with exit 137 — indistinguishable from an OOM kill
        # unless the alert says who was holding the processor.
        if resources.get("cpu"):
            msg += "\n" + t("monitor_top_cpu", list=resources["cpu"])

        # The first question after any death or health flip is "why?" —
        # attach the same log tail the update failures carry. Recovery
        # messages stay clean.
        if kind in ("unhealthy", "exited", "oom", "crash_restart"):
            # For a health flip the probe's own output answers the question
            # directly, and `docker logs` frequently cannot: a container
            # that logs nothing produced an alert with no diagnostic at all
            # (#2, @famewolf). Measured on a probe printing "connection
            # refused: upstream 10.0.0.5:8080" — the healthcheck log had
            # it, `docker logs` was empty. Shown as well as the tail, not
            # instead: they answer different questions.
            probe = ""
            if kind in ("unhealthy", "recovered"):
                probe = (getattr(self, "_latest", None) or {}).get(
                    name, {}).get("health_output", "")
            if probe:
                msg += f"\nHealthcheck:\n```\n{probe}\n```"
            try:
                tail = self.checker._tail_logs(name, lines=10)
            except Exception:
                tail = ""
            if tail:
                msg += f"\nLast logs:\n```\n{tail}\n```"
        try:
            self.bot.send_message(msg, auto=True)
        except Exception as e:
            print(f"Monitor notify error: {e}")
        notifier = getattr(self.bot, "notifier", None)
        if notifier and notifier.has_channels():
            try:
                notifier.send_message(msg)
            except Exception as e:
                print(f"Monitor notifier error: {e}")
        print(f"Monitor: {msg}")
