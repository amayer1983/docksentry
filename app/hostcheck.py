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
  * do the permissions cover what we actually do: list containers and
    inspect one.

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

#: `context://` belongs here and was missing from the first version,
#: which told the podman user who asked for this check that his working
#: endpoint was "not an endpoint I speak" — README calls it the only way
#: to give a remote Podman host its own SSH key.
SCHEMES = ("tcp://", "ssh://", "unix://", "fd://", "npipe://", "context://")

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


#: Every kind `preflight` can emit. Named here rather than only in the
#: comment above, so a test can ask the module instead of keeping its
#: own copy — a kind added without wording would otherwise go unnoticed.
KINDS = ("scheme_bad", "unreachable", "engine", "engine_unsure",
         "can_list", "cannot_list", "can_inspect", "cannot_inspect",
         "no_containers", "ready")


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
    #
    # Plain `version`, not a template: the first attempt asked for
    # `{{.Server.Components}}`, which Docker has and Podman does not —
    # measured on podman 4.9.3, `can't evaluate field Components in type
    # *define.Version`. Every real Podman host therefore came back as
    # "docker", and the test only passed because it fabricated an answer
    # no podman build gives. The plain output names itself on both:
    # "Docker Engine - Community" against "Podman Engine".
    ok_kind, kind, _ = _run(backend, ["version"])
    low = (kind or "").lower()
    if not ok_kind or ("podman" not in low and "docker" not in low):
        # It answered a version and would not say what it is. That is an
        # unanswered question, and this module does not guess at those.
        out.append(("engine_unsure", {"version": ver}))
    else:
        out.append(("engine", {"engine": "podman" if "podman" in low
                               else "docker", "version": ver}))

    ok, listed, err = _run(backend, ["ps", "-aq"])
    if not ok:
        out.append(("cannot_list", {"error": (err or "no answer")[:300]}))
        return out
    ids = [i for i in listed.split("\n") if i.strip()]
    out.append(("can_list", {"count": len(ids)}))

    if not ids:
        # Nothing to inspect is not a failure and does not block the
        # host — a fresh machine is usable. It does mean the permission
        # question stayed open, so it is said out loud instead of being
        # folded into a "ready" that was never earned.
        out.append(("no_containers", {}))
        return out

    ok, raw, err = _run(backend, ["inspect", ids[0]])
    if not ok:
        out.append(("cannot_inspect", {"error": (err or "no answer")[:300]}))
        return out
    # The wording says "name", so show one rather than a truncated id —
    # which is what the reader recognises. The id is the fallback when
    # the answer is not the JSON we expected.
    shown = ids[0][:12]
    try:
        import json as _json
        first = (_json.loads(raw) or [{}])[0]
        shown = (first.get("Name") or shown).lstrip("/") or shown
    except Exception:                                       # noqa: BLE001
        pass
    out.append(("can_inspect", {"name": shown}))
    out.append(("ready", {"endpoint": ep or "local"}))
    return out


def usable(findings):
    """True when nothing in `findings` blocks using the host.

    `no_containers` is NOT a blocker: a fresh host with nothing on it is
    perfectly usable, the inspect right simply could not be exercised
    yet. The finding says so, and this says so too rather than pretending
    the question was answered.

    An empty list is not usable — nothing was measured, and "no findings"
    is the shape a caller gets when it never ran the check.
    """
    bad = {"scheme_bad", "unreachable", "cannot_list", "cannot_inspect"}
    return bool(findings) and not any(kind in bad for kind, _p in findings)
