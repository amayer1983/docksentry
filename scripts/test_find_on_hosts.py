#!/usr/bin/env python3
"""Finding a container across hosts tells "did not answer" from "not there".

`/status web` on an instance with a dead host or one whose SSH refuses
got one of two wrong answers: the timeout raised through the loop, or
the refused `ps` came back with exit 255 and empty output, which the
resolver read as an empty host and answered "Container `web` not
found." Neither says which machine was missing (#2).

`container_flags.find_on_hosts` is the one selection over a list of
hosts, by plain partial name or by glob. Per host it reports exactly one
of three things — the matching names, a Reply saying why there are
none, or that the host did not answer, with the error and the SSH hint
when hostdiag has one — and it hands each host to `on_host` before it
touches the next one. Under it sit `resolve_container_checked` and
`lifecycle.match_glob_checked`, which return the failure (the exception,
or `(returncode, stderr)`) instead of folding it into an empty list.

The hosts are stand-ins at the backend boundary only: `run` returns
what a real CLI returned (a CompletedProcess, or the TimeoutExpired a
dead host raises). The selection, the classification and the hint are
the real code.

Pinned alongside: the old `resolve_container` and `match_glob` still
read a refused host as "not found" / no match. They have callers that
have not moved, and this slice adds without changing them.

Since W7 a read passes `use_cache=True` and `remote_timeout=10`: a
remote host found dead a moment ago is not asked but named with its
remembered words and the seconds left, every remote host that is asked
leaves its outcome in hosts' dead-host memory, and a remote listing gets
ten seconds. Without them (every write) nothing changes. Those checks
run on hosts' clock, replaced.

`--app DIR` runs it against another tree's app/ (that is how it was
seen red before the functions existed).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _app_dir():
    if "--app" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--app") + 1])
    return os.path.join(ROOT, "app")


APP = _app_dir()
sys.path.insert(0, APP)

import container_flags as cf                       # noqa: E402
import errfmt                                      # noqa: E402
import hostdiag                                    # noqa: E402
import lifecycle                                   # noqa: E402
from hosts import ManagedHost                      # noqa: E402

checks = {}

find_on_hosts = getattr(cf, "find_on_hosts", None)
resolve_checked = getattr(cf, "resolve_container_checked", None)
glob_checked = getattr(lifecycle, "match_glob_checked", None)

checks["container_flags.find_on_hosts exists"] = callable(find_on_hosts)
checks["container_flags.resolve_container_checked exists"] = callable(
    resolve_checked)
checks["lifecycle.match_glob_checked exists"] = callable(glob_checked)


def t(key, **kw):
    """A translator that shows what it was asked, so a check can see the
    key AND the parameters went through it."""
    return f"<{key}|{','.join(f'{k}={v}' for k, v in sorted(kw.items()))}>"


EVENTS = []


class Backend:
    """A host at the CLI boundary: `run` answers like the real one."""

    def __init__(self, label, names=(), *, raises=None, rc=0, stderr=""):
        self.label = label
        self.names = list(names)
        self.raises = raises
        self.rc = rc
        self.stderr = stderr
        self.calls = []

    def run(self, args, **kw):
        self.calls.append((list(args), dict(kw)))
        EVENTS.append(("run", self.label))
        if self.raises is not None:
            raise self.raises
        out = "" if self.rc else "".join(f"{n}\n" for n in self.names)
        return subprocess.CompletedProcess(["docker", *args], self.rc,
                                           stdout=out, stderr=self.stderr)


DEAD_ERR = subprocess.TimeoutExpired(
    ["docker", "-H", "ssh://root@dead", "ps", "-a", "--format",
     "{{.Names}}"], 10)
REFUSED_ERR = ("ssh: connect to host refused.lan port 22: "
               "Connection refused\n")
LOCKED_ERR = "ssh: Permission denied (publickey).\n"


def host(name, *, is_local=False, endpoint=""):
    return ManagedHost(name, None, None, None, is_local=is_local,
                       endpoint=endpoint)


LOCAL = host("local", is_local=True)
NAS = host("nas", endpoint="ssh://root@nas.lan")
DEAD = host("dead", endpoint="ssh://root@dead.lan")
REFUSED = host("refused", endpoint="ssh://root@refused.lan")
LOCKED = host("locked", endpoint="ssh://root@locked.lan")


def backends():
    return {
        "local": Backend("local", ["db", "proxy", "proxy2", "web_old"]),
        "nas": Backend("nas", ["web", "nasdb", "webcam"]),
        "dead": Backend("dead", raises=DEAD_ERR),
        "refused": Backend("refused", rc=255, stderr=REFUSED_ERR),
        "locked": Backend("locked", rc=255, stderr=LOCKED_ERR),
    }


HOSTS = [LOCAL, NAS, DEAD, REFUSED]


def run_find(arg, targets=HOSTS, bk=None, **kw):
    """find_on_hosts with a fresh set of backends. Returns (result, raised)."""
    bk = bk if bk is not None else backends()
    EVENTS.clear()
    try:
        res = find_on_hosts(
            targets,
            backend_for=lambda h: bk[getattr(h, "name", None) or "local"],
            arg=arg, t=t, **kw)
        return res, None
    except Exception as e:                              # noqa: BLE001
        return None, e


def names_of(res):
    return [(getattr(h, "name", h), list(n)) for h, n in res.found]


def errors_of(res):
    return [(getattr(r.host, "name", r.host), r.key, dict(r.params))
            for r in res.errors]


def unreachable_of(res):
    return [getattr(u[0], "name", u[0]) for u in res.unreachable]


if callable(find_on_hosts):
    # ── plain name: found on nas only ─────────────────────────────────
    res, raised = run_find("web")
    checks["a dead and a refused host do not raise"] = raised is None
    if res is not None:
        checks["'web' is found on nas only"] = names_of(res) == [
            ("nas", ["web"])]
        checks["…local, which has no web, says not found for itself"] = (
            errors_of(res) == [("local", "resolve_not_found",
                                {"name": "web"})])
        checks["…and dead and refused are unreachable, in walk order"] = (
            unreachable_of(res) == ["dead", "refused"])
        checks["…never 'not found': no Reply names dead or refused"] = not [
            r for r in res.errors
            if getattr(r.host, "name", "") in ("dead", "refused")]
        checks["a per-host Reply carries its host and is not ok"] = all(
            r.host is LOCAL and r.host_is_local and r.ok is False
            for r in res.errors)
        checks["`_old` rollback leftovers are never offered"] = all(
            "web_old" not in n for _, n in res.found)

        # Each entry is the (host, error_text, hint) the plan promised.
        u = {getattr(x[0], "name", ""): tuple(x) for x in res.unreachable}
        checks["an unreachable entry unpacks as (host, error, hint)"] = all(
            len(v) == 3 for v in u.values()) and len(u) == 2
        if sorted(u) == ["dead", "refused"]:
            checks["the dead host's error is the reader's timeout line"] = (
                u["dead"][1] == errfmt.human(DEAD_ERR, t=t)
                == "<error_timed_out|seconds=10>")
            checks["the refused host's error is what ssh said"] = (
                u["refused"][1] == errfmt.clip(REFUSED_ERR)
                and "Connection refused" in u["refused"][1])
            checks["a refused port gets no SSH-key hint (nothing to say)"] = (
                u["refused"][2] == "" and u["dead"][2] == "")
            r = res.unreachable[0]
            reply = r.reply() if hasattr(r, "reply") else None
            checks["an unreachable entry renders as host_unreachable_short"] = (
                reply is not None and reply.key == "host_unreachable_short"
                and reply.params == {"host": "dead", "error": u["dead"][1]}
                and reply.host is DEAD and reply.ok is False
                and reply.host_is_local is False)

    # ── partial on one host, ambiguous on another ─────────────────────
    res, raised = run_find("webc")
    checks["a partial name resolves on its host ('webc' → webcam)"] = (
        raised is None and names_of(res) == [("nas", ["webcam"])])

    res, raised = run_find("prox")
    checks["ambiguous on local is local's resolve_multiple"] = (
        raised is None and errors_of(res)[:1] == [
            ("local", "resolve_multiple",
             {"names": "`proxy`, `proxy2`"})]
        and names_of(res) == [])
    checks["…and nas, which has no prox*, says not found for itself"] = (
        raised is None and errors_of(res)[1:] == [
            ("nas", "resolve_not_found", {"name": "prox"})])
    checks["…while dead and refused still show as unreachable"] = (
        raised is None and unreachable_of(res) == ["dead", "refused"])

    # ── not found anywhere ────────────────────────────────────────────
    res, raised = run_find("zzz")
    checks["not found anywhere: nothing found, first error is local's"] = (
        raised is None and res.found in ((), [])
        and errors_of(res) == [("local", "resolve_not_found",
                                {"name": "zzz"}),
                               ("nas", "resolve_not_found",
                                {"name": "zzz"})])
    checks["…first_error is the first answering host's Reply"] = (
        raised is None and res.first_error is not None
        and res.first_error.key == "resolve_not_found"
        and res.first_error.host is LOCAL
        and res.first_error is res.errors[0])
    res2, _ = run_find("zzz", targets=[DEAD, REFUSED])
    checks["only dead hosts: no first_error, both unreachable"] = (
        res2 is not None and res2.first_error is None
        and res2.errors in ((), []) and res2.found in ((), [])
        and unreachable_of(res2) == ["dead", "refused"])
    res3, _ = run_find("web", targets=[NAS])
    checks["found everywhere: first_error is None"] = (
        res3 is not None and res3.first_error is None)

    # ── glob across hosts ─────────────────────────────────────────────
    res, raised = run_find("*db*")
    checks["a glob matches on every host that has it, in walk order"] = (
        raised is None and names_of(res) == [("local", ["db"]),
                                             ("nas", ["nasdb"])])
    checks["…dead and refused are unreachable, not 'no match'"] = (
        raised is None and unreachable_of(res) == ["dead", "refused"]
        and errors_of(res) == [])
    res, raised = run_find("web*")
    checks["a glob with no match on a host is that host's glob_no_match"] = (
        raised is None and errors_of(res) == [
            ("local", "glob_no_match", {"pattern": "web*"})]
        and names_of(res) == [("nas", ["web", "webcam"])])
    res, raised = run_find("WEB*")
    checks["a glob matches case-insensitively, as /stop web* does"] = (
        raised is None and names_of(res) == [("nas", ["web", "webcam"])])

    # ── include_stopped and timeout reach the CLI ─────────────────────
    bk = backends()
    run_find("*db*", bk=bk, include_stopped=False)
    checks["include_stopped=False drops -a from a glob's ps"] = (
        bk["local"].calls and bk["local"].calls[0][0] == [
            "ps", "--format", "{{.Names}}"])
    bk = backends()
    run_find("*db*", bk=bk)
    checks["a glob lists stopped containers by default"] = (
        bk["local"].calls and bk["local"].calls[0][0] == [
            "ps", "-a", "--format", "{{.Names}}"])
    checks["…and without a timeout the CLI keeps its own default"] = (
        bk["local"].calls and bk["local"].calls[0][1] == {})
    bk = backends()
    run_find("web", bk=bk, timeout=10)
    checks["a timeout is handed to every host's ps"] = all(
        bk[n].calls and bk[n].calls[0][1] == {"timeout": 10}
        for n in ("local", "nas", "dead", "refused"))
    checks["a plain name always lists stopped ones too"] = (
        bk["nas"].calls != []
        and bk["nas"].calls[0][0] == ["ps", "-a", "--format", "{{.Names}}"])

    # ── on_host: once per host, in walk order, before the next ────────
    seen = []

    def on_host(part):
        EVENTS.append(("cb", getattr(part.host, "name", None)))
        seen.append(part)

    res, raised = run_find("web", on_host=on_host)
    checks["on_host runs once per host, each before the next is asked"] = (
        raised is None and EVENTS == [
            ("run", "local"), ("cb", "local"), ("run", "nas"), ("cb", "nas"),
            ("run", "dead"), ("cb", "dead"),
            ("run", "refused"), ("cb", "refused")])
    if len(seen) == 4:
        checks["on_host sees each host's own answer"] = (
            seen[0].names == () and seen[0].error is not None
            and seen[0].error.key == "resolve_not_found"
            and seen[0].unreachable is None
            and tuple(seen[1].names) == ("web",) and seen[1].error is None
            and seen[2].unreachable is not None
            and len(res.unreachable) == 2
            and seen[2].unreachable is res.unreachable[0]
            and seen[3].unreachable is res.unreachable[1])

    # ── the SSH hint ──────────────────────────────────────────────────
    res, raised = run_find("web", targets=[LOCKED])
    want = hostdiag.hint(LOCKED.endpoint, LOCKED_ERR, t=t)
    checks["a refused key gets hostdiag's hint, in the reader's language"] = (
        raised is None and len(res.unreachable) == 1
        and want.startswith("<hostdiag_ssh_")
        and res.unreachable[0][2] == want)

    _orig = hostdiag.hint

    def _boom(*a, **k):
        raise RuntimeError("the hint blew up")

    hostdiag.hint = _boom
    try:
        res, raised = run_find("web", targets=[LOCKED, NAS])
    finally:
        hostdiag.hint = _orig
    checks["a hint that throws costs the hint, not the answer"] = (
        raised is None and unreachable_of(res) == ["locked"]
        and res.unreachable[0][2] == ""
        and names_of(res) == [("nas", ["web"])])

    # ── exit ≠ 0 with nothing on stderr; backend_for that throws ──────
    bk = backends()
    bk["refused"].stderr = ""
    res, raised = run_find("web", targets=[REFUSED], bk=bk)
    checks["a non-zero exit with no stderr says the exit code, translated"] = (
        raised is None and len(res.unreachable) == 1
        and res.unreachable[0][1] == "<error_exit_code|code=255>")

    def _no_backend(h):
        raise RuntimeError("no backend for nas")

    EVENTS.clear()
    try:
        res = find_on_hosts([NAS], backend_for=_no_backend, arg="web", t=t)
        raised = None
    except Exception as e:                              # noqa: BLE001
        res, raised = None, e
    checks["a host whose backend cannot be built is unreachable"] = (
        raised is None and unreachable_of(res) == ["nas"]
        and res.unreachable[0][1] == "no backend for nas")

    # ── backend_for that answers None: no backend, as the old resolvers
    # say it — never "unreachable" quoting Python's NoneType ───────────
    for _arg in ("web", "web*"):
        EVENTS.clear()
        try:
            res = find_on_hosts([NAS], backend_for=lambda h: None, arg=_arg,
                                t=t)
            raised = None
        except Exception as e:                          # noqa: BLE001
            res, raised = None, e
        checks[f"backend_for None is chan_no_backend for {_arg!r}, "
               f"on that host"] = (
            raised is None and res.unreachable == () and res.found == ()
            and errors_of(res) == [("nas", "chan_no_backend", {})]
            and res.errors[0].host is NAS and res.first_error is res.errors[0])

    # A host without a backend comes first in `errors` when it comes
    # first in the walk — ahead of a later host's real "not there".
    _bk = backends()
    EVENTS.clear()
    try:
        res = find_on_hosts(
            [NAS, LOCAL],
            backend_for=lambda h: None if h is NAS else _bk["local"],
            arg="zzz", t=t)
        raised = None
    except Exception as e:                              # noqa: BLE001
        res, raised = None, e
    checks["first_error can be chan_no_backend ahead of a later not-found"] = (
        raised is None and res.found == () and res.unreachable == ()
        and [k for _, k, _ in errors_of(res)] == ["chan_no_backend",
                                                  "resolve_not_found"]
        and res.first_error is res.errors[0])

    # ── a plain name on two hosts at once (/status web, /restart web) ─
    bk = backends()
    bk["local"].names = ["web", "db"]
    res, raised = run_find("web", bk=bk)
    checks["a plain name on two hosts is found on both, in walk order"] = (
        raised is None and names_of(res) == [("local", ["web"]),
                                             ("nas", ["web"])]
        and errors_of(res) == [] and res.first_error is None
        and unreachable_of(res) == ["dead", "refused"])

    # ── a caller's mistake raises; it is not a dead host ──────────────
    bk = backends()
    res, raised = run_find(None, targets=[NAS], bk=bk)
    checks["a bad argument from the caller raises, not 'unreachable'"] = (
        res is None and raised is not None and bk["nas"].calls != [])

    # ── single-host install: the pseudo-host None ─────────────────────
    bk = backends()
    res, raised = run_find("db", targets=[None], bk=bk)
    checks["single host: [None] is walked through backend_for(None)"] = (
        raised is None and names_of(res) == [(None, ["db"])])
    bk["local"].raises = DEAD_ERR
    res, raised = run_find("db", targets=[None], bk=bk)
    u = res.unreachable[0] if res is not None and res.unreachable else None
    checks["single host that fails is 'local', with no hint"] = (
        u is not None and u[0] is None and u[2] == ""
        and u.reply().params["host"] == "local"
        and u.reply().host_is_local is True)


class _Raised:
    """What a checked helper returned when it raised instead: equal to
    nothing, so the check it sits in reads FAIL rather than crashing."""

    def __init__(self, e):
        self.e = e

    def __getitem__(self, i):
        return self

    def __eq__(self, other):
        return False

    __hash__ = object.__hash__

    def __iter__(self):
        return iter((self, self, self))


def _try(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:                              # noqa: BLE001
        print(f"      → {fn.__name__} raised {type(e).__name__}: {e}")
        return _Raised(e)


# ── resolve_container_checked ─────────────────────────────────────────
if callable(resolve_checked):
    _rc = resolve_checked

    def resolve_checked(*a, **kw):                  # noqa: F811
        return _try(_rc, *a, **kw)

    b = backends()
    checks["checked resolve: exact name"] = resolve_checked(
        "web", backend=b["nas"]) == ("web", None, None)
    checks["checked resolve: unique prefix"] = resolve_checked(
        "webc", backend=b["nas"]) == ("webcam", None, None)
    n, r, f = resolve_checked("prox", backend=b["local"])
    checks["checked resolve: ambiguous is resolve_multiple, no failure"] = (
        n is None and f is None and r is not None
        and r.key == "resolve_multiple")
    n, r, f = resolve_checked("zzz", backend=b["local"])
    checks["checked resolve: absent is resolve_not_found, no failure"] = (
        n is None and f is None and r is not None
        and r.key == "resolve_not_found")
    checks["checked resolve: `_old` is filtered"] = resolve_checked(
        "web_old", backend=b["local"])[0] is None
    n, r, f = resolve_checked("web", backend=b["dead"])
    checks["checked resolve: a raise is the failure, the exception itself"] = (
        n is None and r is None and f is DEAD_ERR)
    n, r, f = resolve_checked("web", backend=b["refused"])
    checks["checked resolve: exit 255 is the failure (rc, stderr)"] = (
        n is None and r is None and f == (255, REFUSED_ERR))
    b = backends()
    resolve_checked("web", backend=b["nas"])
    resolve_checked("web", backend=b["local"], timeout=7)
    checks["checked resolve: no timeout → none passed; 7 → timeout=7"] = (
        b["nas"].calls[0][1] == {} and b["local"].calls[0][1] == {
            "timeout": 7})
    # Same names, same rule as the old resolver on an answering host.
    for p in ("web", "webc", "we", "zzz", "WEB"):
        old = cf.resolve_container(p, backend=backends()["nas"])
        new = resolve_checked(p, backend=backends()["nas"])
        checks[f"checked resolve agrees with resolve_container on {p!r}"] = (
            new[:2] == old and new[2] is None)

# ── match_glob_checked ────────────────────────────────────────────────
if callable(glob_checked):
    _gc = glob_checked

    def glob_checked(*a, **kw):                     # noqa: F811
        return _try(_gc, *a, **kw)

    b = backends()
    checks["checked glob: matches, sorted, `_old` filtered"] = glob_checked(
        "*", backend=b["local"]) == (["db", "proxy", "proxy2"], None)
    checks["checked glob: no match on an answering host is ([], None)"] = (
        glob_checked("zz*", backend=b["local"]) == ([], None))
    checks["checked glob: a raise is the failure, the exception itself"] = (
        glob_checked("*", backend=b["dead"]) == ([], DEAD_ERR)
        and glob_checked("*", backend=b["dead"])[1] is DEAD_ERR)
    checks["checked glob: exit 255 is the failure (rc, stderr)"] = (
        glob_checked("*", backend=b["refused"]) == ([], (255, REFUSED_ERR)))
    for pat in ("*", "p*", "PROXY?", "[dp]*", "zz*"):
        for stopped in (True, False):
            ob, nb = backends()["local"], backends()["local"]
            old = lifecycle.match_glob(pat, backend=ob,
                                       include_stopped=stopped)
            new = glob_checked(pat, backend=nb, include_stopped=stopped)
            checks[f"checked glob agrees with match_glob on {pat!r} "
                   f"(include_stopped={stopped})"] = (
                new == (old, None) and ob.calls == nb.calls)

# ── W7: a read skips a remote host found dead a moment ago ────────────
# `use_cache=True` is what a read passes (Telegram's /status <name> and
# /check <name>): a remote host still in its cooldown is not asked, it
# comes back as unreachable with the words it gave last time and how long
# until the next try, and every host that IS asked leaves its outcome in
# the memory. Without it (the default, and every write) the memory is
# neither read nor written. The local host is never skipped or marked:
# the cooldown is for machines on the other end of a wire. The clock is
# hosts' own `time`, replaced, so the seconds do not depend on how fast
# this machine is.
import hosts as hostslib                           # noqa: E402

_CLOCK = [1000.0]


class _Time:
    @staticmethod
    def monotonic():
        return _CLOCK[0]


def _first_kw(backend):
    """The keywords of a backend's first `run`, or None if it was not
    asked — a check that reads FAIL instead of an IndexError."""
    return backend.calls[0][1] if backend.calls else None


def _memory(name):
    left, why = hostslib.unreachable_for(name)
    return (round(left, 3), why)


if callable(find_on_hosts):
    _real_time = hostslib.time
    hostslib.time = _Time
    try:
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("dead", "no answer earlier", cooldown=30.5)
        hostslib.mark_unreachable("local", "local once", cooldown=30.5)
        bk = backends()
        res, raised = run_find("web", bk=bk, use_cache=True)
        checks["W7 cache: a remembered dead host is not asked"] = (
            raised is None and bk["dead"].calls == [])
        u = {getattr(x[0], "name", ""): x for x in
             (res.unreachable if res is not None else ())}
        checks["W7 cache: …it is unreachable with its remembered words and "
               "the whole seconds left"] = (
            "dead" in u and u["dead"][1] == t(
                "host_skipped_retry", error="no answer earlier", seconds=30)
            and u["dead"][2] == "")
        checks["W7 cache: …and renders as host_unreachable_short like any "
               "unreachable host"] = (
            "dead" in u and u["dead"].reply().key == "host_unreachable_short"
            and u["dead"].reply().params["error"] == u["dead"][1])
        checks["W7 cache: the local host is asked even when it is in the "
               "memory"] = (raised is None and len(bk["local"].calls) == 1)
        checks["W7 cache: …and its entry is neither cleared nor renewed"] = (
            _memory("local") == (30.5, "local once"))
        checks["W7 cache: a refused host asked just now is remembered with "
               "the words it gave"] = (
            _memory("refused")[1] == errfmt.clip(REFUSED_ERR)
            and _memory("refused")[0] == 60.0)
        checks["W7 cache: the walk order is kept with a skipped host in it"] = (
            res is not None and unreachable_of(res) == ["dead", "refused"]
            and names_of(res) == [("nas", ["web"])])
        # A host that answers clears its own mark once the cooldown is up,
        # backoff included: its next failure is a first one again (60 s),
        # not the second (120 s).
        hostslib.forget_unreachable()
        run_find("web", targets=[DEAD], use_cache=True)
        first = _memory("dead")
        _CLOCK[0] += 61
        bk = backends()
        bk["dead"] = Backend("dead", ["web"])
        res, raised = run_find("web", targets=[DEAD], bk=bk, use_cache=True)
        checks["W7 cache: a dead host asked just now is remembered for the "
               "first minute"] = first == (60.0, errfmt.human(DEAD_ERR, t=t))
        checks["W7 cache: past its cooldown the host is asked again"] = (
            len(bk["dead"].calls) == 1 and res is not None
            and names_of(res) == [("dead", ["web"])])
        hostslib.mark_unreachable("dead", "x")
        checks["W7 cache: …and answering reset its backoff to the first "
               "minute"] = first[0] == 60.0 and _memory("dead")[0] == 60.0
        # The order is in the calls: each skipped host costs nothing, so
        # on_host still sees every host, and in the order given.
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("nas", "nas earlier", cooldown=10)
        seen = []
        res, raised = run_find("web", targets=[LOCAL, NAS, REFUSED],
                               use_cache=True,
                               on_host=lambda one: seen.append(one))
        checks["W7 cache: on_host sees a skipped host in its place"] = (
            [getattr(s.host, "name", "") for s in seen]
            == ["local", "nas", "refused"]
            and seen[1].unreachable is not None and seen[1].names == ())
        # Without use_cache (every write, and Discord until W7d): the
        # remembered host is asked and nothing is written.
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("dead", "no answer earlier", cooldown=30.5)
        bk = backends()
        res, raised = run_find("web", bk=bk)
        checks["W7 no cache: a remembered dead host is asked anyway"] = (
            len(bk["dead"].calls) == 1)
        checks["W7 no cache: …and nothing is written: refused stays "
               "unknown, dead keeps its old mark"] = (
            _memory("refused") == (0.0, "")
            and _memory("dead") == (30.5, "no answer earlier"))
        # A host without a backend never answered: no mark either way.
        # nas failed once and is past its first minute; had the lookup
        # counted it as answering, its next failure would be a first one
        # (60 s) again instead of the second (120 s).
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("nas", "nas earlier")
        _CLOCK[0] += 61
        res, raised = run_find(
            "web", targets=[NAS], use_cache=True,
            bk={"nas": None, "local": backends()["local"]})
        nas_after = _memory("nas")
        hostslib.mark_unreachable("nas", "nas again")
        checks["W7 cache: a host without a backend leaves the memory "
               "alone"] = (nas_after == (0.0, "")
                           and _memory("nas") == (120.0, "nas again"))
        # A glob takes the same memory.
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("refused", "refused earlier", cooldown=5)
        bk = backends()
        res, raised = run_find("web*", bk=bk, use_cache=True)
        checks["W7 cache: a glob skips a remembered host too"] = (
            bk["refused"].calls == [] and res is not None
            and unreachable_of(res) == ["dead", "refused"])
    finally:
        hostslib.time = _real_time
        hostslib.forget_unreachable()

    # The short probe: `remote_timeout` goes to every host that is not
    # the local one, `timeout` to the local one (None passes nothing).
    bk = backends()
    run_find("web", bk=bk, remote_timeout=10)
    checks["W7 probe: remote_timeout reaches each remote host's ps -a"] = all(
        bk[n].calls and all(kw == {"timeout": 10} for _a, kw in bk[n].calls)
        for n in ("nas", "dead", "refused"))
    checks["W7 probe: …the local host still passes no timeout"] = (
        _first_kw(bk["local"]) == {})
    bk = backends()
    run_find("web*", bk=bk, timeout=5, remote_timeout=10)
    checks["W7 probe: a glob too; `timeout` stays the local one's"] = (
        _first_kw(bk["local"]) == {"timeout": 5}
        and _first_kw(bk["nas"]) == {"timeout": 10})
    bk = backends()
    run_find("web", bk=bk, timeout=7)
    checks["W7 probe: without remote_timeout, `timeout` reaches every "
           "host as before"] = all(
        _first_kw(bk[n]) == {"timeout": 7}
        for n in ("local", "nas", "dead", "refused"))

# ── the old ones are unchanged: a refused host still reads as empty ───
b = backends()
_n, _r = cf.resolve_container("web", backend=b["refused"])
checks["unchanged: resolve_container reads exit 255 as 'not found'"] = (
    _n is None and _r is not None and _r.key == "resolve_not_found")
_n, _r = cf.resolve_container("web", backend=b["dead"])
checks["unchanged: resolve_container folds a raise into chan_list_failed"] = (
    _n is None and _r is not None and _r.key == "chan_list_failed")
checks["unchanged: match_glob reads exit 255 and a raise as no match"] = (
    lifecycle.match_glob("*", backend=b["refused"]) == []
    and lifecycle.match_glob("*", backend=b["dead"]) == [])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print(f"{len(checks) - len(failed)}/{len(checks)}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
