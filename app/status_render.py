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


def _human(n):
    """Bytes the way docker prints them."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.3g}{unit}"
        n /= 1000
    return f"{n:.3g}PB"


def collect(name, state_info, *, stats=None, store=None, pending=None,
            probe="", disk=None):
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
    if disk:
        info.update(disk)
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
    by construction, which is the point.

    Grouped into stanzas rather than a flat label list — the owner's
    reaction to the first version was "können wir das übersichtlicher
    darstellen?", and he was right: eleven `Label: value` lines carry no
    hierarchy, so nothing leads. Now: who and how it is, what it runs,
    what it costs, how it is wired — with a blank line between, because
    a gap is the cheapest heading there is.
    """
    b = lambda s: f"{bold}{s}{bold}"  # noqa: E731
    running = info.get("running")
    health = info.get("health") or ""
    if running:
        icon = "🔴" if health == "unhealthy" else (
            "🟡" if health == "starting" else "✅")
    else:
        icon = "⏸" if info.get("state") == "paused" else "⏹"
    state_text = info.get("state", "?")
    if health:
        state_text += f" ({health})"

    # ── who, and how it is doing ────────────────────────────────
    head = f"🔎 {b(info['name'])}{host_tag} — {icon} {state_text}"
    if running and info.get("uptime"):
        head += f" · ⏱ {info['uptime']}"
    out = [head]
    if not running and info.get("exit_code") not in (None, ""):
        out.append(f"🔻 Exit code `{info['exit_code']}`")
    if info.get("probe"):
        out.append(f"🩺 {b('Health check said:')} `{info['probe']}`")

    # ── what it runs ────────────────────────────────────────────
    img = [f"📦 `{info.get('image', '?')}`"]
    idbits = " · ".join(f"`{info[k]}`" for k in ("version", "short_id")
                        if info.get(k))
    sizebits = []
    if info.get("image_bytes"):
        sizebits.append(_human(info["image_bytes"]))
    if info.get("image_created"):
        age = ""
        try:
            from datetime import date
            d = date.fromisoformat(info["image_created"])
            days = (date.today() - d).days
            if days >= 0:
                age = f", {days}d old"
        except ValueError:
            pass
        sizebits.append(f"built {info['image_created']}{age}")
    if sizebits:
        idbits = " · ".join(x for x in (idbits, " · ".join(sizebits)) if x)
    if idbits:
        img.append(f"     {idbits}")
    out.append("")
    out.extend(img)

    # ── what it costs, right now ────────────────────────────────
    cost = []
    if info.get("cpu") or info.get("mem"):
        cost.append(f"📊 CPU {info.get('cpu', '?')} · "
                    f"RAM {info.get('mem', '?')}")
    if info.get("net_io") or info.get("block_io"):
        cost.append(f"📡 net {_updown(info.get('net_io'))} · "
                    f"disk {_rw(info.get('block_io'))}")
    # A blank line before each cost line, not just before the block —
    # the owner found the paired lines (CPU next to net/disk) read as
    # "gedrückt" against the airy header and image blocks above. One
    # rhythm for the whole detail: every fact gets its own breathing
    # room rather than the lower half clumping into dense pairs.
    for c in cost:
        out.append("")
        out.append(c)

    # ── how it is wired ─────────────────────────────────────────
    wired = []
    ports = _dedupe_ports(info.get("ports"))
    if ports:
        wired.append(f"🌐 {ports}")
    tail = []
    vols = str(info.get("volumes", "") or "")
    if vols and vols not in ("—", "0"):
        tail.append(f"💾 {vols} volume{'s' if vols != '1' else ''}"
                    if vols.isdigit() else f"💾 {vols}")
    # What the container has written on top of its image — the layer
    # that `docker system prune` reclaims, and the one that balloons
    # when something logs into the container filesystem. Volume sizes
    # are deliberately absent: Docker only surfaces them via
    # `system df -v`, which walks every volume on the host (~7 s
    # measured) — a status that stalls answers the wrong question.
    if info.get("layer_bytes") is not None:
        _mark = ""
        if info["layer_bytes"] >= 500 * 1000 * 1000:
            # Past caches-and-temp-files territory: an application is
            # storing data where the next update destroys it.
            _mark = " ⚠ lost on next update"
        tail.append(f"✍ +{_human(info['layer_bytes'])} layer{_mark}")
    if info.get("restart_policy"):
        tail.append(f"🔁 {info['restart_policy']}")
    if tail:
        wired.append(" · ".join(tail))
    for w in wired:
        out.append("")
        out.append(w)

    # ── what Docksentry knows about it ──────────────────────────
    ours = []
    if info.get("flags"):
        ours.append("🏷 " + ", ".join(info["flags"]))
    grp = []
    if info.get("group"):
        grp.append(f"🗂 {b('Group:')} `{info['group']}`")
    if info.get("note"):
        grp.append(f"📝 {info['note']}")
    if grp:
        ours.append(" · ".join(grp))
    if info.get("pending"):
        ours.append("🔄 " + b("Update available"))
    for o in ours:
        out.append("")
        out.append(o)
    return out


def _updown(net):
    """`11MB / 7.15MB` → `↓11MB ↑7.15MB` — docker stats reports NetIO as
    received / sent, in that order. Unparseable input passes through."""
    parts = [p.strip() for p in str(net or "?").split("/")]
    if len(parts) == 2:
        return f"↓{parts[0]} ↑{parts[1]}"
    return str(net or "?")


def _rw(block):
    """`35.8MB / 13.7MB` → `R 35.8MB · W 13.7MB` — BlockIO is
    read / written, in that order."""
    parts = [p.strip() for p in str(block or "?").split("/")]
    if len(parts) == 2:
        return f"R {parts[0]} · W {parts[1]}"
    return str(block or "?")


def _dedupe_ports(ports):
    """`53→53, 53→53` → `53→53` — the same mapping over tcp and udp
    renders as the same text, and one line saying a thing twice reads
    like a mistake (the owner's adguardhome showed exactly that)."""
    if not ports or ports == "—":
        return ""
    seen, out = set(), []
    for part in str(ports).split(","):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return " · ".join(out)


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
