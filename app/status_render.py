"""What `/status` shows, collected once for whichever chat asked (#2).

@NotRetarded compared `/status` on Discord and Telegram and found them
disagreeing — Discord missing Docksentry itself, missing health, missing
uptime, showing `:latest` where a version would mean something. My
own diagnosis of the *cause* was sharper than the report: I assumed
a reply was generated once and then sent per connection, and for
notifications I was right — that is what `announce()` does. Command
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

Not everything here is pure rendering. `collect_overview` is the bare
`/status` overview's host walk and talks to the hosts itself: `ps -q`
and `inspect` on every target through its backend, plus `hostdiag` for
the SSH hint when a host does not answer. It returns data, not text;
counting and markdown stay with the front end. `collect`, `lines` and
`overview_line` never reach a host: inspect data and stats come in from
the caller, `collect` only reads the state store it is handed.
"""

from collections import namedtuple


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

    Grouped into stanzas rather than a flat label list — my own
    reaction to the first version was "können wir das übersichtlicher
    darstellen?", and rightly so: eleven `Label: value` lines carry no
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
        # The rescue helmet, not a stethoscope: this line only appears
        # when a container is actually unhealthy, so the alarm colour is
        # the right one — and it survives Discord's lighter ephemeral
        # background, where the stethoscope washed out (#63,
        # @NotRetarded). It was the wrong icon for the header, which a
        # healthy container also carries; here it is exactly right.
        out.append(f"⛑️ {b('Health check said:')} `{info['probe']}`")

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
    # I found the paired lines (CPU next to net/disk) read as
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
    like a mistake (my adguardhome showed exactly that)."""
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


#: What the /status overview walk found. `inspected`: one (host, inspect
#: dict) per running container, hosts in the order they were given.
#: `unreachable`: one (host name, why) per host that did not answer, the
#: SSH hint under the words when hostdiag has one. The front end does the
#: counting and the rendering.
OverviewData = namedtuple("OverviewData", "inspected unreachable")


def collect_overview(targets, *, backend_for, t, use_cache=False):
    """Walk `targets` for the /status overview: `ps -q`, then `inspect`.

    `targets` None is a single-host install: exactly one pseudo-host
    whose backend is `backend_for(None)` and whose tag is empty, i.e.
    the original two calls. One host after the other, in the order
    given. `t` translates the SSH hint.

    `use_cache=True` is for reads (W7; both chats' /status): a remote host
    still in its cooldown gets no `ps -q` and is listed with its
    remembered words, the seconds until it is asked again and, under
    them, the hint those words get; every remote host that is asked
    leaves its outcome in the memory — the words of its line, without
    the hint under them. The default skips nobody and marks nobody dead,
    but a host that answers is forgotten either way.

    Use docker inspect (not docker ps Status-string parsing) so health
    detection works on both Docker and Podman. Podman's REST API does
    not append `(healthy)` to the Status field — that's a Docker CLI
    cosmetic — but State.Health.Status is consistently provided by
    both. Reported by LeeNX in #28 for podman-compose containers.
    """
    import json
    import hosts as _hosts
    from container_store import LOCAL_HOST
    from errfmt import clip

    inspected = []
    # A host that cannot be reached returns a non-zero `ps` with
    # empty output, which used to be indistinguishable from a host
    # with nothing running: both fell through `continue` and the
    # answer came back as "📊 0 Containers" with no mention that a
    # machine was missing. @famewolf asked `/status @docknas` of an
    # instance whose SSH could not authenticate and got exactly
    # that — the reply neither said the host was unreachable nor
    # which host it was about (#2).
    unreachable = []

    def _mark_unreachable(_host, _why):
        if use_cache:
            _hosts.remember(_host, _why)
        # The host's own diagnostic hint (SSH auth, wrong socket …)
        # appended when hostdiag has one.
        try:
            import hostdiag
            _h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)
            if _h:
                _why += "\n" + _h
        except Exception:
            pass
        unreachable.append(
            (getattr(_host, "name", "") or LOCAL_HOST, _why))

    for _host in (targets or [None]):
        _dead = _hosts.remembered_dead(_host) if use_cache else None
        if _dead is not None:
            _skip = _hosts.skipped_words(*_dead, t=t)
            _h = _hosts.skipped_hint(_host, _dead[0], t=t)
            unreachable.append((getattr(_host, "name", "") or LOCAL_HOST,
                                _skip + ("\n" + _h if _h else "")))
            continue
        _b = backend_for(_host)
        # A dead host does not answer at all: `ps` runs into its
        # timeout and RAISES, where an SSH-auth failure merely
        # returns non-zero. Both mean "unreachable", and neither
        # may take the whole overview down — but a raise did,
        # until a genuinely dead host (srv20) proved it: the
        # timeout escaped this loop into the poll thread and
        # `/status` produced nothing at all (#2).
        # 10s like the Web UI: a host that cannot answer a
        # liveness probe in ten seconds is unreachable for an
        # interactive command — no reason to make everyone
        # wait out the full 30s backend timeout for it.
        try:
            ids_p = _b.run(["ps", "-q"], timeout=10)
        except Exception as _e:
            _mark_unreachable(_host, clip(str(_e)))
            continue
        if getattr(ids_p, "returncode", 0) != 0:
            _mark_unreachable(
                _host,
                clip(getattr(ids_p, "stderr", "") or "")
                or f"exit {ids_p.returncode}")
            continue
        ids = [i for i in ids_p.stdout.strip().split("\n") if i]
        if not ids:
            _hosts.remember(_host, None)
            continue
        try:
            ins_p = _b.run(["inspect", *ids], timeout=10)
        except Exception as _e:
            _mark_unreachable(_host, clip(str(_e)))
            continue
        _hosts.remember(_host, None)
        try:
            _cfgs = json.loads(ins_p.stdout) or []
        except (json.JSONDecodeError, ValueError):
            _cfgs = []
        inspected.extend((_host, _c) for _c in _cfgs)
    return OverviewData(inspected, unreachable)
