"""Updating Docksentry itself — front-end-neutral (#63).

Pulling a target image and recreating our own container: resolving which
version is meant, checking whether it actually changed, writing the
marker the next boot reads, building the helper script that performs the
swap from outside, and recording it in the history. None of that is
Telegram's. It lived on the Telegram bot all the same, which is why
Discord's /selfupdate could only be run by reaching into that instance —
and why, once run, it reported to Telegram and told the person who
started it nothing at all (#63, @NotRetarded).

Fifth step of the core extraction agreed on 2026-08-20, and the largest:
these eleven functions were 486 lines of the Telegram bot.

`ctx` is what the caller supplies, and the contract is exactly four
names — measured, not guessed:

    ctx.t(key, **kw)            the translated text
    ctx.send_message(text)      where the report goes
    ctx.config                  the settings
    ctx.notifier                the other channels, for update results
    ctx._swap_in_flight         set once the helper is launched, so the
                                caller knows not to release the lock on
                                a process that is about to be replaced

The Telegram bot passes itself and behaves exactly as before. Anything
that can answer those five names can drive a self-update — which is the
point: the front end supplies the voice, the core does the work.

This step moves the machinery only. The queue-and-lock coordination
(`_handle_selfupdate`, `_run_queued_selfupdate`) is still on the bot; it
follows next, and that is when Discord stops borrowing.
"""

import json
import os
import shlex
import subprocess

import changelog

def run(ctx, target=None):
    """Body of _handle_selfupdate; only ever called with the update
    lock held (see wrapper above)."""
    # Resolve our own container robustly — handles hosts where $HOSTNAME
    # isn't a directly inspect-resolvable reference (e.g. QNAP Container
    # Station reports "no such object" for it; #41, @NotRetarded).
    from update_checker import UpdateChecker as _UC
    config = _UC.inspect_self()
    if not config:
        ctx.send_message(ctx.t("selfupdate_failed_container"))
        return
    own_name = config["Name"].lstrip("/")
    current_image = config["Config"]["Image"]
    own_image, err = resolve_target(ctx, current_image, target)
    if err:
        ctx.send_message(err)
        return

    # Get current image info
    old_created = config.get("Created", "")[:10]
    old_id_short = config["Image"][:19]

    ctx.send_message(
        ctx.t("selfupdate_checking", image=own_image) + "\n"
        + ctx.t("selfupdate_current_version", date=old_created) + "\n"
        + ctx.t("selfupdate_image_id", id=old_id_short)
    )

    # Pull latest
    pull = subprocess.run(
        ["docker", "pull", own_image],
        capture_output=True, text=True, timeout=300
    )
    if pull.returncode != 0:
        ctx.send_message(ctx.t("selfupdate_failed_pull", error=pull.stderr[:200]))
        return

    # Check if image actually changed
    new_inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}||{{.Created}}", own_image],
        capture_output=True, text=True
    )
    parts = new_inspect.stdout.strip().split("||")
    new_id = parts[0]
    new_created = parts[1][:10] if len(parts) > 1 else "?"
    old_id = config["Image"]

    if new_id == old_id:
        # Same image bits — but if the user explicitly asked to rejoin a
        # rolling tag (`/selfupdate latest`/`stable`) while the container
        # runs a DIFFERENT tag (e.g. a pinned :1.19.0 whose digest equals
        # :latest right now), force a re-tag recreate so the container's
        # image reference actually becomes the moving tag and future
        # updates track it again (#2, @famewolf).
        if should_retag_moving(ctx, target, current_image, own_image):
            ctx.send_message(ctx.t("selfupdate_retag",
                                     old=current_image, new=own_image))
            save_history(ctx, own_name, own_image,
                                          old_created, new_created)
            swap(ctx, config, own_name, own_image)
            return
        # Up to date for the CURRENT image tag. But if the user ran a
        # plain /selfupdate while the container is on a fixed version
        # tag (e.g. :1.19.0) and a newer release exists, plain "up to
        # date" is misleading — they're stuck on an old version that
        # can't self-update past itctx. Guide them. Reported by
        # @famewolf in #2 (his host was stuck on :1.19.0, still hit by
        # the pre-v1.22.0 config-loss bug, with no obvious way out).
        if target is None and ":" in current_image:
            import re
            tag = current_image.rsplit(":", 1)[1]
            m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", tag)
            if m:
                cur_v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                latest_v = latest_released_version(ctx)
                if latest_v and latest_v > cur_v:
                    latest_str = ".".join(str(x) for x in latest_v)
                    ctx.send_message(ctx.t(
                        "selfupdate_old_tag", current=tag, latest=latest_str))
                    return
        ctx.send_message(ctx.t("selfupdate_up_to_date"))
        return

    new_id_short = new_id[:19]
    msg = (
        ctx.t("selfupdate_found") + "\n"
        + version_line(ctx, own_image)
        + ctx.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
        + ctx.t("selfupdate_ids", old=old_id_short, new=new_id_short) + "\n"
        + ctx.t("selfupdate_releases_link") + "\n\n"
        + ctx.t("selfupdate_restarting")
    )
    ctx.send_message(msg)
    # Also fan out to Discord / generic webhook so non-Telegram
    # users actually see self-update events (#19). Same pattern as
    # main.py's startup-message handling.
    if ctx.notifier and ctx.notifier.has_channels():
        ctx.notifier.send_message(msg)

    # Record in history BEFORE _do_selfupdate kills us — otherwise the
    # entry never gets written (#13).
    save_history(ctx, own_name, own_image, old_created, new_created)
    swap(ctx, config, own_name, own_image)


def resolve_target(ctx, current_image, target):
    """Resolve `target` into a fully-qualified image ref to pull.
    Returns (image_ref, error_msg); error_msg is None on success.

    Accepted targets:
        None         → whatever tag the container currently runs
        "latest"     → <base>:latest  (v1.23.4)
        "stable"     → <base>:stable   (v1.23.4)
        "previous"   → last released version older than the running one
        "X.Y.Z"      → a specific semver tag
    """
    if not target:
        return current_image, None

    # Extract base ("registry/owner/repo") from current_image
    if ":" in current_image:
        base = current_image.rsplit(":", 1)[0]
    else:
        base = current_image

    # Moving tags: let the user jump from a pinned :X.Y.Z back onto
    # the rolling :latest / :stable line. Reported by @famewolf in #2
    # — his host was stuck on :1.19.0 and `/selfupdate latest` was
    # rejected, so there was no in-band way to rejoin latest.
    if target.lower() in ("latest", "stable"):
        return f"{base}:{target.lower()}", None

    if target.lower() == "previous":
        # Walk the upstream CHANGELOG for the latest version older
        # than what's currently running — gives the user a one-step
        # rollback target without having to look up version numbers.
        from version import VERSION
        ok, content = changelog.fetch()
        if not ok:
            return None, ctx.t("selfupdate_previous_fetch_failed", error=content)
        import re
        pat = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)
        try:
            cur = tuple(int(x) for x in VERSION.split(".")[:3])
        except ValueError:
            cur = (0, 0, 0)
        best = None
        for m in pat.finditer(content):
            v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if v < cur and (best is None or v > best):
                best = v
        if not best:
            return None, ctx.t("selfupdate_previous_none", current=VERSION)
        target = f"{best[0]}.{best[1]}.{best[2]}"

    # Validate the resolved target is a clean semver — refuses
    # weird input ("latest", "1.2", "1.2.3-rc1") to avoid pulling a
    # tag that the helper container can't actually find.
    import re as _re
    if not _re.match(r"^\d+\.\d+\.\d+$", target):
        return None, ctx.t("selfupdate_invalid_version", version=target)
    return f"{base}:{target}", None


def latest_released_version(ctx):
    """Return the highest version in the upstream CHANGELOG as a
    (major, minor, patch) tuple, or None if it can't be fetched or
    parsed. Used to warn a user pinned to an old :X.Y.Z image tag
    that a newer release exists (#2)."""
    ok, content = changelog.fetch()
    if not ok:
        return None
    import re
    best = None
    for m in re.finditer(r"^## \[(\d+)\.(\d+)\.(\d+)\]", content, re.MULTILINE):
        v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if best is None or v > best:
            best = v
    return best


def should_retag_moving(ctx, target, current_image, own_image):
    """True when the user explicitly asked to move onto a ROLLING tag
    (`latest`/`stable`) and the container is currently on a DIFFERENT
    tag — so even when the pulled digest is unchanged we should recreate
    to actually adopt the moving tag. Closes the SET/UNSET asymmetry:
    `/selfupdate <version>` can pin a tag, but returning to `:latest`
    used to require editing compose on the host because a same-digest
    result short-circuited to "up to date" (#2, @famewolf). False when
    already on that tag — no pointless recreate."""
    return target in ("latest", "stable") and own_image != current_image


def version_line(ctx, target_image):
    """The `v_old → v_new` line for self-update messages (#41 follow-up).
    Old = the running VERSION; new = the target image's
    org.opencontainers.image.version label. Returns "" (line omitted)
    when the new version can't be read — e.g. a pre-label image — so we
    never show a half-blank `v1.33.1 → v?` line."""
    from version import VERSION as _cur
    from update_checker import UpdateChecker as _UC
    new_ver = _UC.image_version_label(target_image)
    if not new_ver:
        return ""
    return ctx.t("selfupdate_versions", old=f"v{_cur}", new=f"v{new_ver}") + "\n"


def save_history(ctx, container_name, image, old_created, new_created):
    """Record a Docksentry self-update in update_history.json so it
    shows up in /history and the Web UI history page alongside
    regular container updates. Reported missing by @famewolf in #13.

    Written BEFORE the helper container restarts us (since we won't
    be alive to write after). Detail uses the same date-arrow format
    as regular container updates so the Web UI doesn't need special
    rendering. success=True is assumed — if the helper fails, the
    next manual /selfupdate will create a follow-up entry with the
    new outcome.

    We know the OLD version (from `version.VERSION`) at this point
    but NOT the NEW version (the new image is pulled but our
    process hasn't restarted yet). Write `v{old} → ?` as a
    placeholder; main.py's post-boot fixup patches the `?` with
    the freshly-booted process's VERSION as part of resuming the
    deferred check (#22)."""
    import json as _json
    from datetime import datetime as _dt
    from version import VERSION as _CUR_VERSION
    entry = {
        "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "container": container_name,
        "image": image,
        "success": True,
        "detail": f"🗓️ {old_created} → {new_created} (selfupdate v{_CUR_VERSION} → ?)",
    }
    try:
        history = []
        if os.path.exists(ctx.config.history_file):
            try:
                with open(ctx.config.history_file) as f:
                    history = _json.load(f)
            except (_json.JSONDecodeError, IOError):
                history = []
        history.append(entry)
        history = history[-100:]
        # Atomic write (v1.22.1) — see container_store.atomic_write_json
        from container_store import atomic_write_json
        atomic_write_json(ctx.config.history_file, history, indent=2)
    except OSError as e:
        print(f"Failed to record selfupdate history: {e}")


def swap(ctx, config, own_name, own_image):
    """Execute selfupdate via a temporary helper container on the host.

    The old approach (Popen + sys.exit) failed because Docker kills all
    processes inside a container when PID 1 exits. Instead, we launch a
    short-lived helper container that runs on the host and performs the
    stop/rename/run/cleanup sequence from outside.
    """
    # Mark that the imminent restart is a self-update. The recreate sends
    # SIGTERM to our old container, which the signal handler records as a
    # generic external stop — without this marker the next boot would
    # mislabel the self-update as "external restart" (#2, @famewolf).
    write_marker(ctx, own_image)
    # Rebuild run command from inspect. Single source of truth via
    # UpdateChecker._build_run_args so the self-update path stays
    # in sync with the regular container-update path (#27): same
    # HostConfig coverage (capabilities, devices, sysctls, tmpfs,
    # extra hosts, DNS, init, shm, log config, ipc/pid/uts modes,
    # runtime, read-only rootfs, user) is preserved on recreate.
    # `_build_run_args` returns the FULL docker run argv including
    # `["docker", "run", "-d", "--name", own_name, ...]` and the
    # image at the end. We slice off the leading `docker run -d`
    # and the trailing image+cmd because the helper-container
    # update_script formats those separately.
    from update_checker import UpdateChecker as _UC
    # Fetch image's default Entrypoint/Cmd so _build_run_args can
    # avoid locking in the OLD image's tokens on update. Best-effort:
    # on failure we pass None which preserves pre-v1.19.0 behaviour.
    image_defaults = None
    try:
        ii = subprocess.run(
            ["docker", "image", "inspect", own_image],
            capture_output=True, text=True, timeout=10,
        )
        if ii.returncode == 0:
            data = json.loads(ii.stdout)
            if data:
                icfg = data[0].get("Config") or {}
                image_defaults = {
                    "Entrypoint": icfg.get("Entrypoint"),
                    "Cmd": icfg.get("Cmd"),
                }
    except (subprocess.SubprocessError, json.JSONDecodeError,
            IndexError, ValueError):
        pass
    # The OLD image's Config, so inherited Env/Labels/User/WorkingDir/
    # StopSignal/Healthcheck aren't pinned onto the new one (#35).
    # `config` is our own container inspect, so .Image is the image we
    # are currently running — the one we're updating away from.
    inherited = _UC._image_config(config.get("Image") or "")
    full = _UC._build_run_args(config, own_image, own_name, image_defaults,
                               inherited=inherited,
                               cgroup_version=_UC._cgroup_version())
    # full = ["docker", "run", "-d", "--name", own_name, ...flags..., own_image, ...cmd...]
    # We need just the flags between "-d" and own_image:
    try:
        img_idx = full.index(own_image)
        run_args = full[3:img_idx]  # drop ["docker","run","-d"] and image+cmd
    except ValueError:
        # Defensive — should never happen since we passed own_image
        run_args = full[3:-1]

    # Build the full recreation command
    run_parts = " ".join(shlex.quote(a) for a in run_args)
    update_script = build_script(
        own_name, run_parts, own_image,
        stop_timeout=max(30, int(getattr(ctx.config,
                                         "docker_stop_timeout", 60) or 60)))

    # Launch a temporary helper container on the host that performs the swap.
    # This container survives because it runs independently on the Docker host.
    helper_name = f"{own_name}_updater"
    # Clean up any leftover helper from a previous attempt
    subprocess.run(["docker", "rm", "-f", helper_name],
                   capture_output=True, timeout=10)

    # v1.22.2: pre-pull `docker:cli` so the helper launch is clean.
    # Previously the implicit auto-pull by `docker run` would write
    # progress to stderr ("Unable to find image 'docker:cli' locally"
    # + layer download lines), and if the auto-pull went sideways
    # (slow registry, transient network blip) the helper-launch
    # subprocess would surface that stderr as the failure message
    # — confusing users when the selfupdate then succeeded anyway.
    # Reported by @NotRetarded in #2.
    helper_pull = subprocess.run(
        ["docker", "pull", "docker:cli"],
        capture_output=True, text=True, timeout=120,
    )
    if helper_pull.returncode != 0:
        ctx.send_message(
            f"❌ Selfupdate failed: couldn't pull helper image `docker:cli` — "
            f"{(helper_pull.stderr or helper_pull.stdout or 'unknown').strip()[:200]}"
        )
        return

    # Mount the SAME host socket Docksentry itself uses (not a hardcoded
    # path) so the helper works on rootless Podman / custom sockets (#43).
    host_sock = host_docker_socket(config)
    helper_args = [
        "docker", "run", "-d",
        "--name", helper_name,
        "--rm",
        "-v", f"{host_sock}:/var/run/docker.sock",
    ]
    # Also mount our /data host dir so the helper can drop its output
    # where the next boot reads it — a --rm helper's stderr otherwise
    # vanishes, which is why we've never seen WHY the podman recreate
    # fails (#43). When mounted, redirect the whole swap script's output
    # into it; the next boot surfaces it if the recreate rolled back.
    host_data = host_mount_source(config, ctx.config.data_dir)
    run_script = update_script
    if host_data:
        helper_args += ["-v", f"{host_data}:{ctx.config.data_dir}"]
        logpath = f"{ctx.config.data_dir}/selfupdate_helper.log"
        run_script = f"({update_script}) > {logpath} 2>&1"
    helper_args += ["docker:cli", "sh", "-c", run_script]
    result = subprocess.run(helper_args, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        ctx.send_message(f"❌ Selfupdate failed: {result.stderr[:200]}")
        return

    # The helper container will stop us in ~3 seconds. Keep the update
    # lock held from here on (see _handle_selfupdate wrapper): nothing
    # may start a container update in the final seconds of this process.
    ctx._swap_in_flight = True
    print(f"Selfupdate helper started ({helper_name}). Waiting for shutdown...")
    import time
    time.sleep(30)


def build_script(name, run_parts, image, stop_timeout=60):
    """Shell run by the helper container to swap Docksentry's image.

    `stop_timeout` is `DOCKER_STOP_TIMEOUT`, and it is here because
    it was missing. A bare `docker stop` uses Docker's own default of
    **ten seconds** and then sends SIGKILL — which is what killed
    @NotRetarded's instance with exit 137 mid-update (#62), while the
    update itself reported success.

    Every other stop in this project was moved onto
    `_lifecycle_timeout()` in v2.8.3 after @famewolf hit exactly this
    on slow containers. This one was missed, in the same way the
    `rename` calls were missed in that release and had to be fixed
    again in 2.8.4. Shutting *ourselves* down is not faster than
    shutting anything else down: the web server, the scheduler and
    the Discord gateway all have to come to a stop first.

    Recovery net (#43, @LeeNX): if the recreate `docker run` fails — e.g. a
    flag the runtime rejects, seen on rootless Podman — the old
    `stop && rename && run && rm` chain left Docksentry DEAD (renamed to
    `_old`, stopped, no new container). The run is now guarded: on failure
    we remove any partial new container, rename `_old` back and start it,
    so the bot survives on the previous version. `rm _old` runs only after
    a successful run, so a failed *cleanup* can't roll back a good update.
    """
    # Quote the container name and image before they land in the `sh -c`
    # script (run_parts is already shlex-quoted by the caller). Docker
    # restricts names/tags to safe characters so shlex.quote is normally
    # a no-op here, but quoting keeps the script robust and consistent
    # with the run-arg handling. `{qname}_old` concatenates fine in sh:
    # an unquoted name stays `ds_old`; a quoted one becomes `'x'_old`,
    # which the shell joins into a single token.
    qname = shlex.quote(name)
    qimage = shlex.quote(image)
    rollback = (
        f"docker rm -f {qname} 2>/dev/null; "
        f"docker rename {qname}_old {qname} 2>/dev/null; "
        f"docker start {qname}"
    )
    # Two failure points, not one. The rollback above covers a failed
    # `docker run`. It does NOT cover a failed `docker rename`, and that
    # is the more dangerous of the two: `rename` fails when `_old`
    # already exists from an interrupted run, the `&&` chain then stops
    # BEFORE the `run`, and the `||` rollback hangs off the run — so it
    # never executes. Docksentry is left stopped, unrenamed, with
    # nothing to bring it back. Dead until someone notices.
    #
    # So: clear any stale backup first, which removes the cause; and if
    # the rename fails anyway, start back up what we just stopped
    # rather than leaving the chain to die quietly. Same defect and the
    # same fix as the container recreate path (watchtower#1101,
    # ouroboros#19/#20).
    return (
        f"sleep 3; "
        f"docker rm -f {qname}_old >/dev/null 2>&1; "
        f"docker stop -t {stop_timeout} {qname} && "
        f"{{ docker rename {qname} {qname}_old || "
        f"{{ echo 'Selfupdate backup failed — restarting unchanged'; "
        f"docker start {qname}; exit 1; }}; }} && "
        f"{{ docker run -d {run_parts} {qimage} || "
        f"{{ echo 'Selfupdate recreate failed — rolling back'; {rollback}; exit 1; }}; }} && "
        f"docker rm {qname}_old"
    )


def write_marker(ctx, image):
    """Record that the imminent restart is a self-update so the next boot
    doesn't mislabel it as an external stop (#2, @famewolf).

    Writes to `ctx.config.selfupdate_marker_file` on the persistent data
    dir (survives the container recreate). Best-effort — a failure only
    costs a cosmetic "external restart" line, never the update itctx.

    NB: this MUST read the path off `ctx.config` (the app Config), not
    off the docker-inspect dict that the selfupdate callers pass around as
    their local `config` — that dict has no such attribute, and the bug
    where the marker write silently `AttributeError`-ed for every
    self-update (manual *and* auto) since v1.26.2 was exactly that mixup.
    """
    try:
        import time as _time
        from container_store import atomic_write_json
        atomic_write_json(ctx.config.selfupdate_marker_file,
                          {"image": image, "ts": _time.time()})
    except Exception as e:
        print(f"Could not write selfupdate marker (non-fatal): {e}")


def host_docker_socket(config):
    """Resolve the HOST path of the Docker/Podman socket this container
    talks to, from our own inspect Mounts. The self-update helper runs
    as a *separate* container and must mount the SAME host socket —
    hardcoding `/var/run/docker.sock` breaks rootless Podman / custom
    socket paths where the real host socket lives elsewhere (e.g.
    `/run/user/1002/podman/podman.sock` mapped to `/var/run/docker.sock`
    inside the container; #43, @LeeNX). Falls back to
    `/var/run/docker.sock` when it can't be determined."""
    inside = "/var/run/docker.sock"
    dh = os.environ.get("DOCKER_HOST", "")
    if dh.startswith("unix://"):
        inside = dh[len("unix://"):] or inside
    mounts = config.get("Mounts") or []
    for m in mounts:
        if m.get("Destination") == inside and m.get("Source"):
            return m["Source"]
    for m in mounts:
        if m.get("Destination") in ("/var/run/docker.sock", "/run/docker.sock") and m.get("Source"):
            return m["Source"]
    return "/var/run/docker.sock"


def host_mount_source(inspect_config, dest):
    """Host source path of the bind/volume mounted at container path
    `dest`, from our own inspect dict, or None. Lets the self-update
    helper mount the SAME host directory Docksentry uses for a path
    (e.g. /data) so it can leave output where the next boot reads it."""
    for m in (inspect_config.get("Mounts") or []):
        if m.get("Destination") == dest and m.get("Source"):
            return m["Source"]
    return None
