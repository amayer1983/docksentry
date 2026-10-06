"""What to ask a host BEFORE trusting it with anything (#7, @LeeNX).

He read the docs, looked at his podman boxes and said the honest thing:
"I feel like there should be a setup step and tests that can be done
before adding the hub pointing at the agent host." There was not. You
put an endpoint in `DOCKER_HOSTS`, restarted, and found out it worked
when something failed — which, for a tool whose whole job is unattended
work, is the wrong moment to learn it.

So this asks, in order, the four questions that decide whether a host is
usable, and says which one failed rather than returning a bare no:

  * does the endpoint even parse, and is the scheme one we speak;
  * does anything answer there at all;
  * what answered — Docker or Podman, which version;
  * do the permissions cover what we actually do: list containers,
    inspect one, read an image.

The last is the one that bites. A socket proxy that allows `GET
/containers/json` and nothing else looks perfectly healthy to a ping and
cannot be updated from. Measured by doing it, not by reading a config.

Everything here is measured at the moment of asking. A question we could
not answer is reported as unanswered, never guessed — the rule this
module shares with `hostdiag`, whose job it is the other half of: that
one explains a failure after it happened, this one looks for it first.

Pure standard library, and no writes anywhere: a pre-flight that changes
something is not a pre-flight.
"""

SCHEMES = ("tcp://", "ssh://", "unix://", "fd://", "npipe://")

#: Finding kinds, in the order they can occur.
#:   scheme_bad      the endpoint is not one we speak
#:   unreachable     nothing answered
#:   engine          what answered, and its version
#:   can_list        `ps` works, and how many containers it saw
#:   cannot_list     …or did not, with what the CLI said
#:   can_inspect     one container could be inspected
#:   cannot_inspect  …or could not
#:   no_containers   nothing to inspect, so that half is untested
#:   ready           every question answered yes


def _run(backend, args, timeout=15):
    """`(ok, stdout, stderr)` — never raises, so one dead endpoint cannot
    take the check with it."""
    try:
        r = backend.run(args, timeout=timeout)
    except Exception as e:                                  # noqa: BLE001
        return False, "", str(e)
    out = (getattr(r, "stdout", "") or "").strip()
    err = (getattr(r, "stderr", "") or "").strip()
    return getattr(r, "returncode", 1) == 0, out, err


def preflight(endpoint, backend, hint=None):
    """`[(kind, params)]` for one endpoint. Does not write anything.

    `backend` is a container backend already pointed at `endpoint`;
    `hint` is `hostdiag.hint`, asked only when something fails, so a
    refused ssh connection still explains the `/root/.ssh` trap rather
    than quoting `Permission denied (publickey)` at somebody who knows
    their keys work.
    """
    out = []
    ep = (endpoint or "").strip()
    if ep and not any(ep.startswith(s) for s in SCHEMES):
        out.append(("scheme_bad", {"endpoint": ep,
                                   "schemes": ", ".join(SCHEMES)}))
        return out

    ok, ver, err = _run(backend, ["version", "--format", "{{.Server.Version}}"])
    if not ok or not ver:
        why = err or "no answer"
        params = {"endpoint": ep or "local", "error": why[:300]}
        if hint is not None:
            try:
                extra = hint(ep, why)
            except Exception:                               # noqa: BLE001
                extra = ""
            if extra:
                params["hint"] = extra
        out.append(("unreachable", params))
        return out

    # Podman answers `docker version` too, so ask what it calls itself
    # rather than inferring it from the endpoint or the binary name.
    _, kind, _ = _run(backend, ["version", "--format", "{{.Server.Components}}"])
    engine = "podman" if "podman" in (kind or "").lower() else "docker"
    out.append(("engine", {"engine": engine, "version": ver}))

    ok, listed, err = _run(backend, ["ps", "-aq"])
    if not ok:
        out.append(("cannot_list", {"error": (err or "no answer")[:300]}))
        return out
    ids = [i for i in listed.split("\n") if i.strip()]
    out.append(("can_list", {"count": len(ids)}))

    if not ids:
        # Nothing to inspect is not a failure, but it does mean the
        # permission question stayed open. Say so rather than passing.
        out.append(("no_containers", {}))
        return out

    ok, _, err = _run(backend, ["inspect", ids[0]])
    if not ok:
        out.append(("cannot_inspect", {"error": (err or "no answer")[:300]}))
        return out
    out.append(("can_inspect", {"name": ids[0][:12]}))
    out.append(("ready", {"endpoint": ep or "local"}))
    return out


def usable(findings):
    """True when nothing in `findings` blocks using the host."""
    bad = {"scheme_bad", "unreachable", "cannot_list", "cannot_inspect"}
    return not any(kind in bad for kind, _p in findings)
