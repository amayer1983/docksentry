"""Container state monitoring (#2, @NotRetarded).

Watches for state *transitions* between scheduler ticks and notifies:

- health went unhealthy (and the recovery back to healthy)
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
        """Transition events between two snapshots. Pure — returns a list
        of (kind, name, detail) tuples. Containers that vanished are NOT
        events (removal is deliberate); new containers just get baselined.
        """
        events = []
        for name, now in cur.items():
            before = prev.get(name)
            if before is None:
                continue

            # health transitions (only containers that have a healthcheck)
            if now["health"] == "unhealthy" and before["health"] != "unhealthy":
                events.append(("unhealthy", name, {"prev": before["health"] or "?"}))
            elif before["health"] == "unhealthy" and now["health"] == "healthy":
                events.append(("recovered", name, {}))

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

        events = [
            (kind, name, detail)
            for kind, name, detail in self.diff(self._prev, cur)
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
            self._notify(kind, name, detail)
            sent.append((kind, name, detail))
        return sent

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
