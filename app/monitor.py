"""Container state monitoring (#2, @NotRetarded).

Watches for state *transitions* between scheduler ticks and notifies:

- health went unhealthy (debounced: confirmed on the next pass before we
  alert, so flappy healthchecks don't spam) and the recovery back to healthy
- container exited with a non-zero code (zero exits are normal endings —
  one-shot jobs would spam otherwise)
- container was OOM-killed
- container crashed and was brought back by its restart policy
  (RestartCount increased while the container kept "running")

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


class ContainerMonitor:
    COOLDOWN_SECONDS = 1800

    def __init__(self, config, checker, bot):
        self.config = config
        self.checker = checker
        self.bot = bot
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
            ps = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30,
            )
            if ps.returncode != 0:
                return None
            names = [n for n in ps.stdout.strip().split("\n") if n]
            if not names:
                return {}
            ins = subprocess.run(
                ["docker", "inspect", *names],
                capture_output=True, text=True, timeout=30,
            )
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

            # running -> exited
            if before["status"] == "running" and now["status"] == "exited":
                if now["oom"]:
                    events.append(("oom", name, {"code": now["exit_code"]}))
                elif now["exit_code"] != 0:
                    events.append(("exited", name, {"code": now["exit_code"]}))
                # zero exit: a normal ending, stay quiet

            # crashed + auto-restarted between ticks: RestartCount went up.
            # Only increases count — a recreate resets the counter to 0.
            if (now["restarts"] > before["restarts"]
                    and now["status"] == "running"):
                if now["oom"]:
                    events.append(("oom", name, {"code": now["exit_code"]}))
                else:
                    events.append(("crash_restart", name,
                                   {"count": now["restarts"]}))
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
            h_now = now["health"]
            h_before = before["health"]

            if h_now == "unhealthy":
                if name in new_alerted:
                    continue                      # already alerted, stay quiet
                if h_before == "unhealthy":
                    # confirmed on a second consecutive pass -> alert now
                    origin = new_pending.pop(name, h_before)
                    events.append(("unhealthy", name, {"prev": origin or "?"}))
                    new_alerted.add(name)
                else:
                    # first flip -> pending; remember the pre-unhealthy state
                    new_pending[name] = h_before or "?"
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

    def tick(self):
        """One monitoring pass. Returns the list of notified events (for
        tests); [] when skipped or quiet."""
        if not getattr(self.config, "monitor_enabled", True):
            return []
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
        self._prev = cur

        sent = []
        now_ts = time.monotonic()
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

    def _memory_snapshot(self, top=3):
        """Top memory consumers RIGHT NOW, as a one-line summary — taken
        only when an OOM event fires (#2, @NotRetarded's Sencho case: the
        OOM alert names the victim, this names the culprit). One
        `docker stats --no-stream` at event time; no idle polling."""
        try:
            r = subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}|{{.MemUsage}}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return ""
            rows = []
            for line in r.stdout.strip().split("\n"):
                if "|" not in line:
                    continue
                cname, usage = line.split("|", 1)
                used = usage.split("/")[0].strip()
                rows.append((self._mem_to_bytes(used), cname.strip(), used))
            rows.sort(reverse=True)
            return " · ".join(f"{cname} {used}" for _, cname, used in rows[:top])
        except (subprocess.SubprocessError, OSError):
            return ""

    def _notify(self, kind, name, detail):
        t = self.bot.t
        if kind == "unhealthy":
            msg = t("monitor_unhealthy", name=name, prev=detail.get("prev", "?"))
        elif kind == "recovered":
            msg = t("monitor_recovered", name=name)
        elif kind == "oom":
            msg = t("monitor_oom", name=name, code=detail.get("code", "?"))
        elif kind == "crash_restart":
            msg = t("monitor_crash_restart", name=name, count=detail.get("count", "?"))
        else:
            msg = t("monitor_exited", name=name, code=detail.get("code", "?"))

        # OOM: name the culprit, not just the victim — one stats snapshot
        # at event time shows who was actually eating the memory.
        if kind == "oom":
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
