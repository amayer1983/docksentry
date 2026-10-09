"""What to ask a host BEFORE trusting it with anything (#7, @LeeNX).

He read the docs, looked at his podman boxes and said the honest thing:
"I feel like there should be a agent setup step and tests that can be
done before adding the hub dockSentry pointing at the agent host."

This is the tests, not the agent. He described an agent setup step and
there is no agent — saying this is "what he asked for" would be claiming
more than was built. What there was before was nothing: you put an
endpoint in `DOCKER_HOSTS`, restarted, and found out it worked when
something failed — which, for a tool whose whole job is unattended work,
is the wrong moment to learn it.

So this asks, in order, the four questions that decide whether a host is
usable, and says which one failed rather than returning a bare no:

  * does the endpoint even parse, and is the scheme one we speak;
  * does anything answer there at all;
  * what answered — Docker or Podman, which version;
  * can we list containers, and inspect one.

Note the limit, because the wording used to overstate it: every question
here is a READ. Updating also needs to pull an image and recreate a
container, and nothing short of doing it can prove those are allowed —
so a socket proxy that permits GET and refuses POST passes this whole
check and fails the first update with a 403. The `ready` finding says so
rather than promising the host "can be managed".

The read side is still the one that bites most often: a socket proxy
that allows `GET /containers/json` and nothing else looks perfectly
healthy to a ping and cannot be inspected, let alone updated from. That
is found by asking it, not by reading a config.

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

#: What the "that is not an endpoint I speak" message offers. A subset of
#: what is ACCEPTED, because two of those cannot work from in here and
#: suggesting them sends the reader down a dead end: `fd://` is systemd
#: socket activation handing a descriptor to the process it started, and
#: `npipe://` is a Windows named pipe. Docksentry is an Alpine container
#: on Linux. They stay accepted — somebody with an unusual setup may
#: know better than this list — but they are not advice.
SUGGEST = ("tcp://", "ssh://", "unix://", "context://")

#: Finding kinds, in the order they can occur.
#:   scheme_bad      the endpoint is not one we speak
#:   unreachable     nothing answered
#:   denied          the socket answered, and refused us
#:   denied_ssh      ssh answered, and would not let us in
#:   engine          what answered, and its version
#:   engine_unsure   …or would not say what it is
#:   can_list        `ps` works, and how many containers it saw
#:   cannot_list     …or did not, with what the CLI said
#:   can_inspect     one container could be inspected
#:   cannot_inspect  …or could not
#:   no_containers   nothing to inspect, so that half is untested
#:   gone            everything it listed had finished by the time we
#:                   asked, so that half is untested too
#:   ready           every question answered yes


#: Every kind `preflight` can emit. Named here rather than only in the
#: comment above, so a test can ask the module instead of keeping its
#: own copy — a kind added without wording would otherwise go unnoticed.
#: Every kind that can appear in a rendered pre-flight — the eleven
#: `preflight` emits plus `usable`, which is `verdict`'s. Kept as one
#: list because what reads it is the wording check, and a kind without
#: wording renders as a bare key wherever it comes from.
KINDS = ("scheme_bad", "unreachable", "denied", "denied_ssh",
         "engine", "engine_unsure",
         "can_list", "cannot_list", "can_inspect", "cannot_inspect",
         "no_containers", "gone", "ready", "usable")

#: What `preflight` itself can emit — everything above except `usable`.
#: Separate, so a test can hold each list to its own promise instead of
#: one list standing for two different claims.
EMITTED = tuple(k for k in KINDS if k != "usable")


#: Words that mean "I heard you and you may not" — an actual refusal.
#:
#: This list was wider and that was worse than having none. It held
#: "cannot connect to the docker daemon at unix", which the docker cli
#: prints both when the rights are missing AND when nothing is listening
#: at all ("Is the docker daemon running?"). A stopped daemon was then
#: reported as "it answered and refused us: check the mount, the group,
#: and SELinux", when the answer is `systemctl start docker` and nothing
#: had answered. Three separate audits found it by three different
#: routes. So: a real permission word, or it is not a refusal.
_REFUSED = ("permission denied", "access denied", "not permitted",
            "unauthorized", "must be root", "forbidden",
            "permission or group", "insufficient privileges")

#: Phrases that say SSH itself turned us away, as opposed to the
#: daemon behind it. The difference is the whole advice: an ssh refusal
#: is a key, a daemon refusal over the same ssh connection is the
#: socket's group.
_SSH_REFUSED = ("ssh:", "publickey", "password)", "keyboard-interactive",
                "host key verification", "remote host identification",
                "no supported authentication")


def _ssh_refused(text):
    """Did SSH refuse us, rather than something on the far side?

    Decided on the words, not on the scheme — which is how the first
    version got it wrong. `ssh://bob@srv30` where the login works and
    `bob` is not in the `docker` group produces "permission denied while
    trying to connect to the Docker daemon socket at unix://…", and
    reading that as a key problem sends the reader to his
    `authorized_keys` when the answer is `usermod -aG docker bob`. That
    is the most ordinary `ssh://` failure there is, and before the split
    it was answered correctly.
    """
    low = (text or "").lower()
    if "docker daemon socket" in low or "unix://" in low:
        return False            # the far side's socket, over ssh
    return any(k in low for k in _SSH_REFUSED)


def _refused(text):
    """Did something answer and say no, as opposed to not answering?

    One list, deliberately. There was a second one here — phrases that
    mean "nothing was listening", checked first — and it was both
    unexercised and wrong: removing the narrow list above is what fixes
    the stopped-daemon misdiagnosis, and in the one case where a message
    carries a connect phrase AND a permission word (rootless Podman:
    "Cannot connect to Podman … permission denied") that really is a
    permission problem, so letting the connect phrase win would have
    reintroduced the same class of error facing the other way.
    """
    low = (text or "").lower()
    return any(k in low for k in _REFUSED)


#: Per-question timeout. 30, not 15, because that is what the scheduler
#: gives a host in normal operation — and a pre-flight that is stricter
#: than production tells somebody his working host is unreachable. A box
#: that answers in twenty seconds was called dead here and ran fine
#: afterwards, which is the one answer a pre-flight must never give.
#:
#: The cost is the other way round: four questions at 30 seconds is two
#: minutes in the worst case, and on Telegram that blocks the poller.
#: See OPEN.md — the fix for that is to take the check off the listening
#: thread, not to make it lie faster.
PROBE_TIMEOUT = 30


def _run(backend, args, timeout=PROBE_TIMEOUT):
    """`(ok, stdout, stderr)` — never raises, so one dead endpoint cannot
    take the check with it."""
    try:
        r = backend.run(args, timeout=timeout)
    except Exception as e:                                  # noqa: BLE001
        return False, "", str(e)
    out = (getattr(r, "stdout", "") or "").strip()
    err = (getattr(r, "stderr", "") or "").strip()
    return getattr(r, "returncode", 1) == 0, out, err


def _gone(text):
    """Did `inspect` fail because that container no longer exists?

    Both engines say so in words rather than in an exit code. It matters
    because "it is not there any more" says nothing about the host, while
    every other inspect failure says something about its rights.
    """
    low = (text or "").lower()
    return "no such container" in low or "no such object" in low


#: How much of a typed endpoint goes back into the answer.
#:
#: `{error}` was already bounded at 300 and `{endpoint}` was not, so a
#: 4000-character endpoint was the one input that made the two chats
#: disagree: Telegram split the reply into pieces, Discord clipped it at
#: 1900. The findings are the core's job and so is their size.
MAX_ENDPOINT = 200


def short_endpoint(endpoint):
    """The endpoint as it goes into a message, bounded.

    Exported because clipping it inside `preflight` was not enough and
    I said it was: both renderers put the RAW endpoint into their header
    line and their closing line, so a 4000-character endpoint still made
    the two chats disagree — measured, Telegram 8231 characters over
    five lines, Discord clipped to 1900 with not one finding left in it.
    The reader got a truncated address and nothing else.

    One function, used by the module and by whoever renders it, so there
    is one answer to "how long can this get".
    """
    return (endpoint or "")[:MAX_ENDPOINT]


def preflight(endpoint, backend, hint=None):
    """`[(kind, params)]` for one endpoint. Does not write anything.

    `backend` is a container backend already pointed at `endpoint`;
    `hint` is `hostdiag.hint`, asked when the FIRST question fails — the
    one where the endpoint does not answer or refuses us. That is where
    the `/root/.ssh` trap shows up: an ssh host whose keys work on the
    machine and not in the container. The hint is added to the error, not
    instead of it; the reader gets both the words the cli used and what
    they tend to mean. A failure further in (`cannot_list`,
    `cannot_inspect`) gets no hint, because `hostdiag` only has sentences
    about ssh and by then ssh has demonstrably worked.
    """
    out = []
    ep = (endpoint or "").strip()
    if ep and not any(ep.startswith(s) for s in SCHEMES):
        out.append(("scheme_bad", {"endpoint": short_endpoint(ep),
                                   "schemes": ", ".join(SUGGEST)}))
        return out

    ok, ver, err = _run(backend, ["version", "--format", "{{.Server.Version}}"])
    if not ok or not ver:
        why = err or "no answer"
        params = {"endpoint": short_endpoint(ep) or "local", "error": why[:300]}
        if hint is not None:
            try:
                extra = hint(ep, why)
            except Exception:                               # noqa: BLE001
                extra = ""
            if extra:
                params["hint"] = extra
        # Split by scheme, because "you may not" means two different
        # things and the advice is not interchangeable. Over a socket it
        # is the mount, the group, SELinux. Over ssh it is a key, and
        # telling that reader to check his socket group sends him the
        # wrong way — while `hostdiag` appends the right sentence
        # directly underneath, so he got both at once.
        if not _refused(why):
            kind = "unreachable"
        elif _ssh_refused(why):
            # Not `ep.startswith("ssh://")`: `context://` reaches a
            # remote Podman over ssh too — the README calls it the only
            # way to give one its own key — so the scheme does not say
            # who refused, and the words do.
            kind = "denied_ssh"
        else:
            kind = "denied"
        out.append((kind, params))
        return out

    # Podman answers `docker version` too, so ask what it calls itself
    # rather than inferring it from the endpoint.
    #
    # Not from the binary name either, but that needs saying carefully:
    # the plain output carries a `Client:` block as well, which names the
    # LOCAL cli. Measured here with the docker cli against a podman
    # service — "Client: Docker Engine - Community" and "Podman Engine:"
    # in one answer. Both names present, so the order of the test below
    # is the whole decision, and it is deliberate: "podman" first,
    # because the docker cli says "docker" about itself on every host it
    # ever talks to, while nothing says "podman" unless podman is in the
    # conversation.
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

    # More than one candidate, because `ps -aq` lists the NEWEST first
    # and that is exactly the one most likely to be gone a moment later:
    # a `--rm` backup job or CI runner that finished between the two
    # calls. Judging the host on `ids[0]` alone turned that into "a
    # container cannot be inspected — a socket proxy that allows listing
    # but not inspecting looks healthy and cannot be updated from", for
    # a host whose rights were fine. Measured: three containers, the
    # first one gone, verdict `cannot_inspect` and not usable.
    raw = err = ""
    ok = False
    every_one_gone = True
    for _id in ids[:3]:
        ok, raw, err = _run(backend, ["inspect", _id])
        if ok:
            ids = [_id] + [i for i in ids if i != _id]
            every_one_gone = False
            break
        if not _gone(err):
            every_one_gone = False
            break            # a real refusal: no point asking again
    if not ok:
        if every_one_gone:
            # Three for three finished between the two calls. That says
            # nothing about the host — it says this box runs short-lived
            # jobs — so it must not read as "a socket proxy that allows
            # listing but not inspecting", and it must not block. The
            # inspect right simply stayed untested, same as on a host
            # with nothing on it.
            out.append(("gone", {}))
            return out
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
    out.append(("ready", {"endpoint": short_endpoint(ep) or "local"}))
    return out


def verdict(findings):
    """The closing line's kind, or None when the findings carry their own.

    `ready` is already a verdict, so a run that reached it needs nothing
    more. The case this is for is `no_containers`: nothing blocked, so
    the host is usable, but the inspect question stayed open and there is
    no `ready` to say so. That used to end with three findings and no
    conclusion, leaving the reader to work out for himself whether he
    could add the host — which is the one thing he came to ask.
    """
    kinds = {k for k, _p in findings}
    if "ready" in kinds or not usable(findings):
        return None
    return "usable"


def usable(findings):
    """True when nothing in `findings` blocks using the host.

    `no_containers` is NOT a blocker: a fresh host with nothing on it is
    perfectly usable, the inspect right simply could not be exercised
    yet. The finding says so, and this says so too rather than pretending
    the question was answered.

    An empty list is not usable — nothing was measured, and "no findings"
    is the shape a caller gets when it never ran the check.
    """
    # `denied` belongs here and was missed when it was added: a socket
    # that answers and refuses us is the most blocking answer there is,
    # and leaving it out reported a host with no rights as usable.
    bad = {"scheme_bad", "unreachable", "denied", "denied_ssh",
           "cannot_list", "cannot_inspect"}
    return bool(findings) and not any(kind in bad for kind, _p in findings)
