"""One container's live facts — state, stats, disk — front-end-neutral (#63).

`state()`, `stats()` and `disk_facts()` read a container's inspect / stats
and return plain dicts and tuples for `status_render` to lay out. They
lived on the Telegram bot, and the Discord bot reached into that instance
to borrow all three — but none of it is Telegram's: it runs docker/podman
against a backend and shapes the result. Both front ends call it here now.

`backend` is required and first: whichever host's backend the caller wants
these facts from. (It used to default to the Telegram bot's own backend;
every caller already passes one explicitly, so the default only hid the
dependency.) Second step of the core extraction agreed on 2026-08-20.
"""

import json
import subprocess


def state(backend, name):
    """Return a dict with current state of `name` for the per-container
    status output. Keys: state, health, uptime, image, ports, volumes,
    restart_policy. All values are strings (already formatted for
    display). Returns None if inspect fails.

    `backend` picks the host to inspect on (#7)."""
    try:
        r = backend.run(
            ["inspect", name], timeout=10)
        if r.returncode != 0:
            return None
        cfg = json.loads(r.stdout)[0]
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return None

    state = cfg.get("State", {}) or {}
    host = cfg.get("HostConfig", {}) or {}
    config = cfg.get("Config", {}) or {}

    status = state.get("Status", "?")
    health = (state.get("Health") or {}).get("Status", "")

    # Uptime from StartedAt
    started_at = state.get("StartedAt", "")
    uptime = "?"
    if state.get("Running") and started_at:
        try:
            from datetime import datetime as _dt, timezone as _tz
            s = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = _dt.now(_tz.utc) - s
            total = int(delta.total_seconds())
            if total < 60:
                uptime = f"{total}s"
            elif total < 3600:
                uptime = f"{total // 60}m {total % 60}s"
            elif total < 86400:
                uptime = f"{total // 3600}h {(total % 3600) // 60}m"
            else:
                days = total // 86400
                hrs = (total % 86400) // 3600
                uptime = f"{days}d {hrs}h"
        except (ValueError, AttributeError):
            pass

    # Ports — only host-mapped ones
    port_lines = []
    for cport, bindings in (host.get("PortBindings") or {}).items():
        if bindings:
            for b in bindings:
                hp = b.get("HostPort", "")
                if hp:
                    port_lines.append(f"{hp}→{cport.split('/')[0]}")
    ports = ", ".join(port_lines) if port_lines else "—"

    # Volumes count
    mounts = cfg.get("Mounts", []) or []
    volumes = f"{len(mounts)}"

    restart = (host.get("RestartPolicy") or {}).get("Name", "no")

    # Resolved version + image hash (#36, @LeeNX — same data the Web UI
    # detail shows since #32). Both come from the already-fetched inspect,
    # so no extra docker call. The OCI version label is the honest answer
    # to "what version is this really?" beyond a rolling :latest tag.
    image_id = cfg.get("Image", "") or ""
    short_id = image_id[7:19] if image_id.startswith("sha256:") else image_id[:12]
    # Version from the RUNNING image's own label, not the container's:
    # pre-v1.43.0 recreates pinned stale version labels onto containers
    # (#46, @LeeNX — /status kept showing the old version after every
    # selfupdate). The image can't lie about itself; the container can.
    version = ""
    if image_id:
        r = backend.run(
            ["image", "inspect", "--format",
             '{{index .Config.Labels "org.opencontainers.image.version"}}',
             image_id], timeout=10)
        if r.returncode == 0:
            version = r.stdout.strip()
    if not version:
        version = ((config.get("Labels") or {}).get(
            "org.opencontainers.image.version") or "").strip()

    return {
        "name": cfg.get("Name", "").lstrip("/"),
        "state": status,
        "health": health,
        "uptime": uptime,
        "image": config.get("Image", "?"),
        "version": version,
        "short_id": short_id,
        "ports": ports,
        "volumes": volumes,
        "restart_policy": restart or "no",
        "running": bool(state.get("Running")),
        # How it died, for a container that is not running — the
        # first thing worth knowing about a stopped service, and the
        # field #62 was diagnosed from (137 = SIGKILL).
        "exit_code": state.get("ExitCode"),
    }


def stats(backend, name):
    """(cpu, mem) as Docker prints them, or None. One bounded call —
    `docker stats` without --no-stream would sit forever."""
    try:
        r = backend.run(["stats", "--no-stream", "--format",
                   "{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}",
                   name], timeout=20)
    except Exception:
        return None
    out = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0 or "|" not in out:
        return None
    parts = [p.strip() for p in out.split("|")]
    # Four fields since net/disk I/O joined; a Podman that answers
    # with fewer still yields the two that matter.
    return tuple(parts[:4]) if len(parts) >= 2 else None


def disk_facts(backend, name):
    """Image size and age, plus the container's writable layer.

    Detail view only, never the overview loop: two extra inspects
    per container would double the overview's cost, while one
    container's detail absorbs them without noticing (measured:
    ~13 ms together).

    Volume sizes are deliberately NOT here. The only way Docker
    offers them is `system df -v`, which walks every volume on the
    host — measured at ~7 s on this machine — and a status command
    that stalls for seconds answers a different question than it
    was asked.
    """
    out = {}
    try:
        r = backend.run(["inspect", "--size", "--format",
                   "{{.Image}}|{{.SizeRw}}", name], timeout=15)
        if getattr(r, "returncode", 1) == 0 and "|" in (r.stdout or ""):
            image_id, sizerw = r.stdout.strip().split("|", 1)
            if sizerw.strip().lstrip("-").isdigit():
                out["layer_bytes"] = int(sizerw)
            r2 = backend.run(["image", "inspect", "--format",
                        "{{.Size}}|{{.Created}}", image_id.strip()],
                       timeout=15)
            if getattr(r2, "returncode", 1) == 0 and "|" in (r2.stdout or ""):
                size, created = r2.stdout.strip().split("|", 1)
                if size.strip().isdigit():
                    out["image_bytes"] = int(size)
                out["image_created"] = created.strip()[:10]
    except Exception:
        pass
    return out
