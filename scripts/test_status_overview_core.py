#!/usr/bin/env python3
"""The /status overview's host walk is the core's, and it reads as before.

Bare `/status` on Telegram walked its hosts inside the command handler:
`ps -q` and `inspect` against each host, one after the other, ten
seconds each, and a host that did not answer — a dead one raises its
timeout, a refused SSH returns exit 255 — went into an "unreachable"
list with the SSH hint under it. Until a genuinely dead host proved
otherwise, the raise escaped the loop and `/status` said nothing at all
(#2).

`status_render.collect_overview(targets, *, backend_for, t)` is that
walk, lifted unchanged: sequential, `ps -q` with timeout=10, `inspect`
with timeout=10, `hostdiag.hint(..., t=t)` inside its own try. It
returns `OverviewData(inspected, unreachable)`: one (host, inspect dict)
per running container in host order, and one (host name, why) per host
that did not answer. Telegram keeps the formatting, the summary, the
bot uptime, the queue line and the chunking.

The hosts are the Telegram matrix's (T2) registry: local, nas, dead
(TimeoutExpired), refused (exit 255 + ssh stderr), flaky. Only `run`
is a stand-in; the walk, the classification and the hint are real.
T2's /status rows pin the rendered output byte for byte; this pins what
the core hands back.

Since W7 Telegram passes `use_cache=True`: a remote host found dead a
moment ago gets no `ps -q` and is listed with its remembered words and
the seconds left, and each host asked leaves its outcome in hosts'
dead-host memory (without the hint). Discord does not, yet (W7d).

`--app DIR` runs it against another tree's app/ (that is how it was
seen red before the function existed).
"""
import os
import shutil
import subprocess
import sys
import tempfile

import test_telegram_dispatch_matrix as m

checks = {}
m._TMP_ROOT = tempfile.mkdtemp(prefix="overview-core-")


def world(setup, modes=None, refused=None):
    """T2's hosts for `setup`, every `run` also written to one shared
    list so the order across hosts is visible."""
    real = m.MatrixBackend.refused_stderr
    m.MODE_OVERRIDE.update(modes or {})
    if refused is not None:
        m.MatrixBackend.refused_stderr = refused
    try:
        cfg, store, managed, registry, backends, data = m.build_world(setup)
    finally:
        m.MODE_OVERRIDE.clear()
        m.MatrixBackend.refused_stderr = real
    if refused is not None:
        for b in backends.values():
            b.refused_stderr = refused
    order = []
    for name, b in backends.items():
        inner = b.run

        def run(args, *a, _inner=inner, _name=name, **k):
            order.append((_name, [str(x) for x in args][:2], k.get("timeout")))
            return _inner(args, *a, **k)
        b.run = run
    return registry, backends, order


def backend_for_of(backends):
    """What Telegram's `_backend_for` does: None and the local host are
    the bot's own backend, every other host its own."""
    return lambda h: backends["local"] if (h is None or h.is_local) \
        else h.backend


try:
    import status_render
    from i18n import get_translator
    import hostdiag
    en, de = get_translator("en"), get_translator("de")
    real_collect = getattr(status_render, "collect_overview", None)
    checks["status_render.collect_overview exists"] = callable(real_collect)
    raised = []

    def collect(*a, **k):
        """The real walk; a raise out of it becomes a red check here
        rather than a traceback that ends the procedure."""
        try:
            return real_collect(*a, **k)
        except Exception as e:                          # noqa: BLE001
            raised.append(f"{type(e).__name__}: {e}")
            return status_render.OverviewData([], [])

    if callable(real_collect):
        # ── multi: rows per host, the dead and refused host listed ──────
        registry, backends, order = world("multi")
        ov = collect(list(registry), backend_for=backend_for_of(backends),
                     t=en)
        rows = [(h.name, c["Name"].lstrip("/")) for h, c in ov.inspected]
        checks["one row per running container, hosts in registry order"] = (
            rows == [("local", "docksentry"), ("local", "web"),
                     ("local", "db"), ("nas", "web"), ("nas", "media"),
                     ("flaky", "web"), ("flaky", "cache")])
        checks["…each row carries its host object, not a name"] = all(
            h is registry.get(h.name) for h, _ in ov.inspected)
        dead_why = ("Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' "
                    "timed out after 10 seconds")
        checks["the dead and the refused host are listed, in order"] = (
            ov.unreachable == [("dead", dead_why),
                               ("refused", m.MatrixBackend.refused_stderr)])
        if not checks["the dead and the refused host are listed, in order"]:
            print(f"      → {ov.unreachable!r}")
        checks["every host asked once with ps -q, ten seconds"] = all(
            [c for c in order if c[0] == n][:1]
            == [(n, ["ps", "-q"], 10)] for n in backends)
        checks["inspect on the hosts that answered, ten seconds"] = (
            [(n, a[0], t) for n, a, t in order if a[0] == "inspect"]
            == [("local", "inspect", 10), ("nas", "inspect", 10),
                ("flaky", "inspect", 10)])
        checks["one host after the other, in registry order"] = (
            [n for n, _, _ in order]
            == ["local", "local", "nas", "nas", "dead", "refused",
                "flaky", "flaky"])

        # ── the SSH hint, in the reader's language ──────────────────────
        no_dir = os.path.join(m._TMP_ROOT, "no-ssh-here")
        denied = "user@192.0.2.30: Permission denied (publickey)."
        real_home = hostdiag._home_ssh
        hostdiag._home_ssh = lambda: no_dir
        try:
            registry, backends, _o = world("multi", refused=denied)
            ov = collect([registry.get("refused")],
                         backend_for=backend_for_of(backends), t=de)
        finally:
            hostdiag._home_ssh = real_home
        hint_de = de("hostdiag_ssh_no_dir", dir=no_dir)
        checks["the SSH hint stands under the refused host's words"] = (
            ov.unreachable == [("refused", denied + "\n" + hint_de)])
        checks["…and it is in the reader's language"] = (
            hint_de != en("hostdiag_ssh_no_dir", dir=no_dir))

        # ── a hint that blows up costs the hint, not the overview ───────
        real_hint = hostdiag.hint

        def boom(*a, **k):
            raise RuntimeError("hint broke")
        hostdiag.hint = boom
        try:
            registry, backends, _o = world("multi")
            ov = collect(list(registry), backend_for=backend_for_of(backends),
                         t=en)
        finally:
            hostdiag.hint = real_hint
        checks["a hint that raises leaves the host listed without it"] = (
            [n for n, _ in ov.unreachable] == ["dead", "refused"]
            and ov.unreachable[1][1] == m.MatrixBackend.refused_stderr
            and len(ov.inspected) == 7)

        # ── single host: one pseudo-host, the bot's own backend ─────────
        registry, backends, order = world("none")
        ov = collect(None, backend_for=backend_for_of(backends), t=en)
        checks["no registry: one walk over the bot's own backend"] = (
            [(h, c["Name"].lstrip("/")) for h, c in ov.inspected]
            == [(None, "docksentry"), (None, "web"), (None, "db")]
            and [(a[0], t) for _n, a, t in order]
            == [("ps", 10), ("inspect", 10)]
            and ov.unreachable == [])
        gone = ("Cannot connect to the Docker daemon at unix:///var/run/"
                "docker.sock. Is the docker daemon running?")
        registry, backends, order = world("none", modes={"local": "refused"},
                                          refused=gone)
        ov = collect(None, backend_for=backend_for_of(backends), t=en)
        checks["no registry, daemon refuses: listed as local"] = (
            ov.inspected == [] and ov.unreachable == [("local", gone)])

        # ── the edges of one host ───────────────────────────────────────
        class Host:
            def __init__(self, name, run):
                self.name, self.is_local, self.endpoint = name, False, ""
                self.backend = type("B", (), {"run": staticmethod(run)})()

        def cp(rc=0, out="", err=""):
            return subprocess.CompletedProcess([], rc, out, err)

        def by(**answers):
            calls = []

            def run(args, timeout=None):
                calls.append(args[0])
                a = answers[args[0]]
                if isinstance(a, Exception):
                    raise a
                return a
            return run, calls

        run, calls = by(ps=cp(1, "", ""))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["exit != 0 with nothing on stderr says the exit code"] = (
            ov.unreachable == [("x", "exit 1")] and calls == ["ps"])
        run, calls = by(ps=cp(0, "\n\n"))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["nothing running: no inspect, nothing listed"] = (
            ov.inspected == [] and ov.unreachable == [] and calls == ["ps"])
        run, calls = by(ps=cp(0, "abc\n"), inspect=OSError("socket closed"))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["inspect raising makes the host unreachable"] = (
            ov.unreachable == [("x", "socket closed")] and ov.inspected == [])
        run, calls = by(ps=cp(0, "abc\n"), inspect=cp(0, "not json"))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["unreadable inspect output: no rows, not unreachable"] = (
            ov.inspected == [] and ov.unreachable == [])
        long_err = "x" * 900
        run, calls = by(ps=cp(255, "", long_err))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["the error is clipped like every other host line"] = (
            len(ov.unreachable) == 1 and len(ov.unreachable[0][1]) == 400)
        # The two raise paths clip as well: a dead host's timeout text and
        # a broken socket's are no shorter than a refused host's stderr.
        run, calls = by(ps=OSError(long_err))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["…and so is a raising ps's"] = (
            len(ov.unreachable) == 1 and len(ov.unreachable[0][1]) == 400)
        run, calls = by(ps=cp(0, "abc\n"), inspect=OSError(long_err))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["…and a raising inspect's"] = (
            len(ov.unreachable) == 1 and len(ov.unreachable[0][1]) == 400)
        # `inspect` answering `null` is valid JSON with nothing in it; it
        # must read as no rows, not raise out of the walk.
        before = len(raised)
        run, calls = by(ps=cp(0, "abc\n"), inspect=cp(0, "null"))
        ov = collect([Host("x", run)], backend_for=lambda h: h.backend, t=en)
        checks["inspect answering null: no rows, no raise"] = (
            len(raised) == before
            and ov.inspected == [] and ov.unreachable == [])

        # Measured on the old handler and kept: a backend that cannot be
        # had at all is the caller's error, not an unreachable host.
        def no_backend(h):
            raise LookupError("no backend")
        try:
            real_collect([Host("x", None)], backend_for=no_backend, t=en)
            checks["unchanged: backend_for raising is not swallowed"] = False
        except LookupError:
            checks["unchanged: backend_for raising is not swallowed"] = True
        checks["nothing above raised out of the walk"] = not raised
        if raised:
            print(f"      → {raised}")

        # ── W7: a read skips a remote host found dead a moment ago ──────
        # `use_cache=True` is what Telegram's /status passes: a remote
        # host still in its cooldown gets no `ps -q`, and is listed with
        # the words it gave last time and how long until the next try;
        # every host that is asked leaves its outcome in the memory (the
        # words without the hint — the hint is said under the line, not
        # remembered). The local host is never skipped or marked. Without
        # use_cache (Discord until W7d) nothing is read or written.
        import hosts as hostslib
        _clock = [9000.0]

        class _Time:
            @staticmethod
            def monotonic():
                return _clock[0]

        def _mem(name):
            left, why = hostslib.unreachable_for(name)
            return (round(left, 3), why)

        _real_time = hostslib.time
        hostslib.time = _Time
        before = len(raised)
        try:
            hostslib.forget_unreachable()
            hostslib.mark_unreachable("dead", "no answer earlier",
                                      cooldown=30.5)
            hostslib.mark_unreachable("local", "local once", cooldown=30.5)
            registry, backends, order = world("multi")
            ov = collect(list(registry), backend_for=backend_for_of(backends),
                         t=en, use_cache=True)
            checks["W7 cache: a remembered dead host gets no ps -q"] = (
                len(raised) == before
                and not [c for c in order if c[0] == "dead"])
            checks["W7 cache: …it is listed in its place with its remembered "
                   "words and the seconds left, rounded up"] = (
                ov.unreachable == [
                    ("dead", en("host_skipped_retry",
                                error="no answer earlier", seconds=31)),
                    ("refused", m.MatrixBackend.refused_stderr)])
            checks["W7 cache: the hosts that answered are listed as before"] = (
                len(ov.inspected) == 7)
            checks["W7 cache: the local host is asked even when it is in the "
                   "memory, and its entry is left alone"] = (
                [c for c in order if c[0] == "local"][:1]
                == [("local", ["ps", "-q"], 10)]
                and _mem("local") == (30.5, "local once"))
            checks["W7 cache: the refused host asked just now is remembered "
                   "with its words"] = (
                _mem("refused") == (60.0, m.MatrixBackend.refused_stderr))
            # A raise is remembered with the line's words, and the hint
            # under a line is not part of what is remembered.
            hostslib.forget_unreachable()
            real_home = hostdiag._home_ssh
            hostdiag._home_ssh = lambda: no_dir
            try:
                registry, backends, order = world("multi", refused=denied)
                ov = collect(list(registry),
                             backend_for=backend_for_of(backends), t=en,
                             use_cache=True)
            finally:
                hostdiag._home_ssh = real_home
            checks["W7 cache: a dead host asked just now is remembered with "
                   "its line's words"] = _mem("dead") == (60.0, dead_why)
            checks["W7 cache: …a refused one without the hint under its "
                   "line"] = (
                _mem("refused") == (60.0, denied)
                and ("refused", denied + "\n" + en("hostdiag_ssh_no_dir",
                                                   dir=no_dir))
                in ov.unreachable)
            # An inspect that raises counts as a failure of that host; a
            # host that answers both forgets its mark, backoff included.
            hostslib.forget_unreachable()
            run, calls = by(ps=cp(0, "abc\n"), inspect=OSError("socket closed"))
            collect([Host("x", run)], backend_for=lambda h: h.backend, t=en,
                    use_cache=True)
            checks["W7 cache: a raising inspect is remembered too"] = (
                _mem("x") == (60.0, "socket closed"))
            _clock[0] += 61
            run, calls = by(ps=cp(0, "abc\n"), inspect=cp(0, "[]"))
            collect([Host("x", run)], backend_for=lambda h: h.backend, t=en,
                    use_cache=True)
            hostslib.mark_unreachable("x", "again")
            checks["W7 cache: past its cooldown it is asked, and answering "
                   "reset its backoff"] = (
                calls == ["ps", "inspect"] and _mem("x")[0] == 60.0)
            # Nothing running is an answer too.
            _clock[0] += 61
            hostslib.mark_unreachable("x", "again")
            _clock[0] += 121
            run, calls = by(ps=cp(0, "\n"))
            collect([Host("x", run)], backend_for=lambda h: h.backend, t=en,
                    use_cache=True)
            hostslib.mark_unreachable("x", "once more")
            checks["W7 cache: a host with nothing running answered, and is "
                   "forgotten"] = calls == ["ps"] and _mem("x")[0] == 60.0
            # Without use_cache: asked anyway, nothing written.
            hostslib.forget_unreachable()
            hostslib.mark_unreachable("dead", "no answer earlier",
                                      cooldown=30.5)
            registry, backends, order = world("multi")
            ov = collect(list(registry), backend_for=backend_for_of(backends),
                         t=en)
            checks["W7 no cache: a remembered host is asked anyway"] = (
                [c for c in order if c[0] == "dead"]
                == [("dead", ["ps", "-q"], 10)])
            checks["W7 no cache: …and nothing is written"] = (
                _mem("dead") == (30.5, "no answer earlier")
                and _mem("refused") == (0.0, ""))
            # In its last second a host is still skipped, so it says 1s,
            # not "not asked again for 0s" (review of W7: int() said 0).
            hostslib.forget_unreachable()
            hostslib.mark_unreachable("x", "boom", cooldown=0.4)
            run, calls = by(ps=cp(0, "abc\n"), inspect=cp(0, "[]"))
            ov = collect([Host("x", run)], backend_for=lambda h: h.backend,
                         t=en, use_cache=True)
            checks["W7 cache: in its last second a skipped host says 1s, "
                   "not 0s"] = (
                calls == [] and ov.unreachable == [
                    ("x", en("host_skipped_retry", error="boom", seconds=1))])
            # A timeout's raw line, remembered, is said the way a fresh
            # one would be: errfmt.human's words, not Python's argv.
            hostslib.forget_unreachable()
            hostslib.mark_unreachable("x", dead_why)
            ov = collect([Host("x", run)], backend_for=lambda h: h.backend,
                         t=en, use_cache=True)
            checks["W7 cache: a remembered timeout is said as \"no answer "
                   "within 10 seconds\", not as the argv"] = (
                calls == [] and ov.unreachable == [
                    ("x", en("host_skipped_retry",
                             error=en("error_timed_out", seconds="10"),
                             seconds=60))])
            # The memory is the one the Web status page reads (hosts.walk):
            # a host Telegram's /status found dead is skipped there too,
            # with the words /status remembered, and a failure here moves
            # the same backoff a failure there would.
            hostslib.forget_unreachable()
            run, calls = by(ps=subprocess.TimeoutExpired(
                ["docker", "-H", "ssh://dead", "ps", "-q"], 10))
            collect([Host("x", run)], backend_for=lambda h: h.backend, t=en,
                    use_cache=True)
            asked = []
            walked = hostslib.walk([Host("x", run)],
                                   lambda h: asked.append(h) or [])
            checks["W7 shared memory: a host /status found dead is skipped by "
                   "the Web page's walk, with /status's words"] = (
                asked == [] and len(walked) == 1 and walked[0].skipped
                and walked[0].reason == dead_why
                and walked[0].retry_in == 60
                and hostslib._failures.get("x") == 1)
            _clock[0] += 61
            collect([Host("x", run)], backend_for=lambda h: h.backend, t=en,
                    use_cache=True)
            checks["W7 shared memory: …and a second /status failure doubles "
                   "the cooldown the page sees"] = (
                _mem("x") == (120.0, dead_why)
                and hostslib._failures.get("x") == 2)
            checks["W7: nothing in the cache checks raised out of the walk"] = (
                len(raised) == before)
        finally:
            hostslib.time = _real_time
            hostslib.forget_unreachable()

    # ── Telegram asks the core, and renders what it gets ───────────────
    calls = []

    def recording(targets, **k):
        calls.append((targets, sorted(k) if k.get("use_cache") is True
                      else ["use_cache is not True"]))
        return real_collect(targets, **k)
    if callable(real_collect):
        status_render.collect_overview = recording
    try:
        rec, esc = m.run_row("multi", "/status")
    finally:
        if callable(real_collect):
            status_render.collect_overview = real_collect
    # With the dead-host memory (W7): bare /status is a read.
    checks["Telegram /status asks status_render.collect_overview once, "
           "with the cache"] = (
        len(calls) == 1 and calls[0][1] == ["backend_for", "t", "use_cache"]
        and [h.name for h in calls[0][0]]
        == ["local", "nas", "dead", "refused", "flaky"])
    checks["…and the overview it sends is T2's, byte for byte"] = (
        rec == m.EXPECTED[("multi", "/status")] and not esc)
finally:
    shutil.rmtree(m._TMP_ROOT, ignore_errors=True)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print(f"{len(checks) - len(failed)}/{len(checks)}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
