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


def _clock(iso):
    """`HH:MM:SS` from Docker's RFC3339 timestamp, or "" when absent.

    Only the time of day: the alert lands within a minute of the event, so
    a date adds noise, and a full RFC3339 string with nanoseconds is
    unreadable in a chat message. Anything unparseable degrades to empty
    rather than printing a raw timestamp at the user.
    """
    if not iso or not isinstance(iso, str):
        return ""
    # "2026-08-01T16:19:25.123456789Z" → the time part, seconds precision.
    try:
        t = iso.split("T", 1)[1]
        return t.split(".", 1)[0].rstrip("Z")[:8]
    except (IndexError, AttributeError):
        return ""



class ContainerMonitor:
    COOLDOWN_SECONDS = 1800

    def __init__(self, config, checker, bot, backend=None):
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
            }
        return snap

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
            w = EventWatcher(self.backend, self._memory_snapshot,
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
        for kind, name, detail in events:
            key = (name, kind)
            last = self._last_sent.get(key, 0)
            if now_ts - last < self.COOLDOWN_SECONDS:
                continue
            self._last_sent[key] = now_ts
            self._record(kind, name, detail)
            self._notify(kind, name, detail)
            sent.append((kind, name, detail))
        return sent

    MAX_EVENTS = 200

    def _record(self, kind, name, detail):
        """Append to the persistent event log (shown in the Web UI's
        Events section). Telegram scrollback is no audit trail, and on
        headless installs without notification channels this file is the
        only place "what happened last night?" can be answered at all.
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
            events.append({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "container": name,
                "detail": detail,
            })
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
        try:
            vals = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    num = rest.strip().split(" ")[0]
                    if num.isdigit():
                        vals[key] = int(num) * 1024      # kB → bytes
        except OSError:
            return ""
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

    def _memory_snapshot(self, top=3):
        """Top memory consumers RIGHT NOW, as a one-line summary.

        Attached to any death — OOM, a crash-restart, or a plain non-zero
        exit — because "which container did this to me?" is the same
        question in all three, and a container squeezed out by a neighbour
        does not necessarily get killed by the kernel's OOM killer (#2,
        @NotRetarded: his Unifi container died from a neighbour without an
        OOM flag, so the snapshot that could have named the culprit never
        fired). One `docker stats --no-stream` at event time — about two
        seconds on twenty containers, which is why it hangs off an event
        and never off a poll."""
        rows = getattr(self, "_snap_cache", None)
        if rows is None:
            rows = []
            try:
                r = self.backend.stats(fmt="{{.Name}}|{{.MemUsage}}",
                                       timeout=30)
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        if "|" not in line:
                            continue
                        cname, usage = line.split("|", 1)
                        used = usage.split("/")[0].strip()
                        rows.append(
                            (self._mem_to_bytes(used), cname.strip(), used))
                    rows.sort(reverse=True)
            except (subprocess.SubprocessError, OSError):
                rows = []
            # Cached even when empty: a daemon that just refused one stats
            # call will refuse the next nineteen too, and a burst of events
            # should not turn into a burst of doomed subprocesses.
            self._snap_cache = rows
        return " · ".join(f"{cname} {used}" for _, cname, used in rows[:top])

    def _notify(self, kind, name, detail):
        t = self.bot.t
        if kind == "unhealthy":
            msg = t("monitor_unhealthy", name=name, prev=detail.get("prev", "?"))
        elif kind == "recovered":
            msg = t("monitor_recovered", name=name)
        elif kind == "oom":
            msg = t("monitor_oom", name=name, code=detail.get("code", "?"))
        elif kind == "crash_restart":
            # `code`/`when` are extra format kwargs: a translation that
            # hasn't picked them up yet simply ignores them, so no language
            # can break on this.
            msg = t("monitor_crash_restart", name=name,
                    count=detail.get("count", "?"),
                    code=detail.get("code", "?"),
                    when=detail.get("when", "") or "?")
        else:
            msg = t("monitor_exited", name=name, code=detail.get("code", "?"))

        # Name the culprit, not just the victim. This used to fire for OOM
        # only, which missed the commonest shape of the problem: a
        # container squeezed out by a neighbour often dies without the
        # kernel OOM-killing it, so the one alert that could have pointed
        # at the neighbour stayed silent.
        if kind in ("oom", "crash_restart", "exited"):
            host_mem = self._host_memory()
            if host_mem:
                msg += "\n" + t("monitor_host_memory", state=host_mem)
            # Evidence from the event stream first: it was taken at the
            # moment of death, while the culprit still held the memory.
            # Falling back to a fresh snapshot keeps the old behaviour
            # wherever the watcher is off, unavailable, or too late.
            snap = ""
            watcher = getattr(self, "watcher", None)
            if watcher:
                snap = watcher.evidence(name)
            if not snap:
                snap = self._memory_snapshot()
            if snap:
                msg += "\n" + t("monitor_top_memory", list=snap)

        # The first question after any death or health flip is "why?" —
        # attach the same log tail the update failures carry. Recovery
        # messages stay clean.
        if kind in ("unhealthy", "exited", "oom", "crash_restart"):
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
