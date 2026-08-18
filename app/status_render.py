"""One container detail, rendered for whichever chat asked (#2).

@NotRetarded compared `/status` on Discord and Telegram and found them
disagreeing — Discord missing Docksentry itself, missing health, missing
uptime, showing `:latest` where a version would mean something. The
owner's diagnosis of the *cause* was sharper than the report: he assumed
a reply was generated once and then sent per connection, and for
notifications he is right — that is what `announce()` does. Command
replies were the exception: each front end assembled its own, which is
two assemblies, which is drift. This module ends the exception for the
detail view: the fields are collected once, and each front end only
chooses its markdown.

What a detail shows, in order — the answer to "what would make sense
here", which is exactly the set of questions you have when a container
misbehaves at 2 am:

  * state, health and uptime — is it running, is it well, since when
  * exit code when it is not running — how it died
  * CPU and memory, live — what it is costing right now
  * what the health probe SAID when unhealthy — the 2.15.0 lesson:
    the probe's words, not the container's
  * image, version label, image id — `latest` is a name, not a version
  * ports, volumes, restart policy
  * Docksentry's own knowledge: pinned, auto-update, protected,
    trust-running, ask-major, group, note, pending update
"""


def collect(name, state_info, *, stats=None, store=None, pending=None,
            probe=""):
    """Merge everything known about one container into a flat dict.

    `state_info` is `_container_state()`'s dict (inspect-derived).
    `stats` is (cpu, mem) strings or None. `store` is the host's state
    view. Every part is optional: a missing store just means no flag
    lines, not a crash — remote hosts answer inspect long before their
    state is wired up.
    """
    info = dict(state_info or {})
    info["name"] = info.get("name") or name
    if stats:
        # Two fields or four: CPU and memory always, net and disk I/O
        # when the runtime reports them — `docker stats` hands all four
        # over in the same call, so the extra two cost nothing.
        info["cpu"], info["mem"] = stats[0], stats[1]
        if len(stats) >= 4:
            info["net_io"], info["block_io"] = stats[2], stats[3]
    if probe:
        info["probe"] = probe
    if store is not None:
        # Per accessor, not per store: the docstring above promises that
        # a missing piece costs a line, never the whole status — and the
        # first run against a test's minimal store broke that promise on
        # `get_protect_stop`. A detail view that dies because one flag
        # source is absent has its priorities backwards.
        def _members(accessor):
            try:
                return getattr(store, accessor)() or []
            except Exception:
                return []

        flags = [label for accessor, label in (
            ("get_pinned", "pinned"),
            ("get_autoupdate", "auto-update"),
            ("get_protect_stop", "protected"),
            ("get_trust_running", "trust-running"),
            ("get_ask_before_major", "ask-major"),
        ) if name in _members(accessor)]
        info["flags"] = flags
        for gid, g in (dict(_members("get_groups")) or {}).items():
            if name in (g.get("containers") or []):
                info["group"] = g.get("name", gid)
                break
        note = dict(_members("get_notes")).get(name)
        if note:
            info["note"] = note
    if pending is not None:
        info["pending"] = bool(pending)
    return info


def lines(info, *, bold="*", host_tag=""):
    """The detail as chat lines. `bold` is the only front-end difference:
    Telegram wants `*x*`, Discord `**x**`. Everything else is identical
    by construction, which is the point."""
    b = lambda s: f"{bold}{s}{bold}"  # noqa: E731
    running = info.get("running")
    icon = "✅" if running else ("⏸" if info.get("state") == "paused" else "⏹")
    state_text = info.get("state", "?")
    if info.get("health"):
        state_text += f" ({info['health']})"

    out = [f"📊 {b(info['name'])}{host_tag}  {icon}",
           f"{b('State:')} `{state_text}`"]
    if not running and info.get("exit_code") not in (None, ""):
        out.append(f"{b('Exit code:')} `{info['exit_code']}`")
    if running and info.get("uptime"):
        out.append(f"⏱ {b('Uptime:')} {info['uptime']}")
    if info.get("cpu") or info.get("mem"):
        out.append(f"📈 {b('Load:')} CPU {info.get('cpu', '?')} · "
                   f"RAM {info.get('mem', '?')}")
    if info.get("net_io") or info.get("block_io"):
        out.append(f"📡 {b('I/O:')} net {info.get('net_io', '?')} · "
                   f"disk {info.get('block_io', '?')}")
    if info.get("probe"):
        out.append(f"🩺 {b('Health check said:')} `{info['probe']}`")
    out.append(f"{b('Image:')} `{info.get('image', '?')}`")
    if info.get("version"):
        out.append(f"{b('Version:')} `{info['version']}`")
    if info.get("short_id"):
        out.append(f"{b('Image ID:')} `{info['short_id']}`")
    if info.get("ports"):
        out.append(f"{b('Ports:')} {info['ports']}")
    if info.get("volumes"):
        out.append(f"{b('Volumes:')} {info['volumes']}")
    if info.get("restart_policy"):
        out.append(f"{b('Restart policy:')} `{info['restart_policy']}`")
    if info.get("flags"):
        out.append(f"{b('Docksentry:')} " + ", ".join(info["flags"]))
    if info.get("group"):
        out.append(f"{b('Group:')} `{info['group']}`")
    if info.get("pending"):
        out.append("🔄 " + b("Update available"))
    if info.get("note"):
        out.append(f"📝 {info['note']}")
    return out


def overview_line(name, state_info, *, host_tag="", version=""):
    """One container, one line, for the overview list.

    The version label when the image has one, because `:latest` is a
    name and not a version — @NotRetarded's ollama read
    `ollama/ollama:latest` where `v0.32.14` was sitting in the label.
    """
    si = state_info or {}
    running = si.get("running")
    icon = "🟢" if (si.get("health") == "healthy") else (
        "🔴" if si.get("health") == "unhealthy" else (
            "🟡" if si.get("health") == "starting" else (
                "⚪" if running else "⏹")))
    bits = [f"{icon} `{name}`{host_tag}"]
    ver = version or si.get("version") or ""
    img = si.get("image", "")
    bits.append(f"`{img}`" + (f" ({ver})" if ver else ""))
    if running and si.get("uptime"):
        bits.append(f"⏱ {si['uptime']}")
    return " — ".join(bits)
