#!/usr/bin/env python3
"""The Telegram update check walks its hosts in one place.

Telegram had the same loop four times: the arg-less `/check`
(`_run_full_check`), `/check --dry-run`, `/check <name>` and
`/update <name>`. Each asked every host's checker in turn, and each had
the same rule for a host that raised: on a single-host install the
exception goes up as it always did, with several hosts the host is
reported as "could not be checked" and the walk goes on (#7). Only the
arg-less `/check` put hostdiag's SSH hint under the failure.

`container_flags.check_on_hosts(targets, *, checker_for, only_for=None,
debug_sink=None, hint=False, on_host=None, raise_alone=True, t)` is that
loop. It hands
`on_host(HostCheck)` each host's answer before the next host's
`check_all` starts, which is what lets a chat say "nas: up to date" the
moment nas is done (#2, @famewolf), and it returns the same answers as
a list. What to SAY about an answer stays with the chat.

The first half drives the function with checkers that record how they
were called. The second half drives Telegram itself on the dispatch
matrix's hosts (T2: local, nas, dead, refused, flaky) and holds each of
the four commands to its T2 row, byte for byte, plus where the SSH hint
appears and where it does not. Only hostdiag's view of `~/.ssh` (and,
once, the hint itself) is replaced, never the decision whether to ask.

Discord's /check had a fifth copy (W6d): no hint, `check_all()` bare,
the error cut to 80 characters, and it never raised, not even without a
registry. It walks through check_on_hosts now with `raise_alone=False`;
the last part holds its rows to T4 (the Discord matrix) and drives the
no-registry case directly.

The Web UI's "Check Updates" had a sixth (W6w): `check_all()` bare, a
raise printed per host, the findings notified once at the end. Its rows
are held to T5 (the Web matrix), and the handler is driven directly
without a registry and on one and two hosts.

Since W7 Telegram's /check, /check --dry-run and /check <name> pass
`use_cache=True`: a remote host found dead a moment ago is not checked
again, only a host that could not list its containers is remembered,
and /update never takes the memory. The core is driven on hosts' clock,
replaced; the Telegram sites are held to which of them pass it.

`--app DIR` runs it against another tree's app/ (that is how it was
seen red before the function existed).
"""
import os
import shutil
import sys
import tempfile

import test_telegram_dispatch_matrix as m   # puts --app / app on sys.path

import container_flags as cf                 # noqa: E402
import hostdiag                              # noqa: E402
from i18n import get_translator              # noqa: E402

checks = {}
check_on_hosts = getattr(cf, "check_on_hosts", None)
checks["container_flags.check_on_hosts exists"] = callable(check_on_hosts)

EVENTS = []


class Host:
    def __init__(self, name, endpoint=""):
        self.name = name
        self.endpoint = endpoint or f"tcp://{name}:2375"
        self.is_local = name == "local"


class Checker:
    """A host's checker at the check_all boundary: records the call
    (with the keywords it got, so "not passed" is visible) and answers
    or raises."""

    def __init__(self, label, result=None, raises=None):
        self.label = label
        self.result = [] if result is None else result
        self.raises = raises
        self.calls = []

    def check_all(self, **kw):
        self.calls.append(dict(kw))
        EVENTS.append(("check_all", self.label))
        if self.raises is not None:
            raise self.raises
        return self.result


class _Raised:
    def __init__(self, exc):
        self.exc = exc


def _try(fn, *a, **k):
    """`fn(...)`, or a `_Raised` — a raise is a red check here, not a
    traceback."""
    try:
        return fn(*a, **k)
    except Exception as e:                                  # noqa: BLE001
        return _Raised(e)


def _walk(targets, checkers, **kw):
    """check_on_hosts over `targets`, `checkers` keyed by host name (None
    for the single pseudo-host); every on_host call lands in EVENTS."""
    del EVENTS[:]
    seen = []

    def on_host(hc):
        seen.append(hc)
        EVENTS.append(("on_host", getattr(hc.host, "name", None)))
    kw.setdefault("on_host", on_host)
    kw.setdefault("t", get_translator("en"))
    got = _try(check_on_hosts, targets,
               checker_for=lambda h: checkers[getattr(h, "name", None)],
               **kw)
    return got, seen


SSH_DENIED = "ssh: user@192.0.2.30: Permission denied (publickey)."

if callable(check_on_hosts):
    # ── several hosts: one raises, the walk goes on ──────────────────
    a, b, c = Host("local"), Host("nas", "ssh://user@192.0.2.30"), Host("pve")
    found = [{"name": "web"}]
    boom = OSError(SSH_DENIED)
    ck = {"local": Checker("local", found), "nas": Checker("nas", raises=boom),
          "pve": Checker("pve", [])}
    sink = object()
    got, seen = _walk([a, b, c], ck, debug_sink=sink)
    ok = isinstance(got, list) and len(got) == 3
    checks["three hosts, three answers, in the order given"] = (
        ok and [hc.host for hc in got] == [a, b, c])
    if not ok:
        print(f"      → got {got!r}")
    checks["a host's updates come back as check_all returned them"] = (
        ok and got[0].updates is found and got[0].error is None)
    checks["a host that raised comes back with its exception, not raised"] = (
        ok and got[1].error is boom and got[1].updates is None)
    checks["…and the walk went on to the next host"] = (
        ok and got[2].error is None and got[2].updates == []
        and ck["pve"].calls != [])
    checks["each answer carries the checker it came from"] = (
        ok and [hc.checker for hc in got]
        == [ck["local"], ck["nas"], ck["pve"]])
    checks["no hint unless one is asked for"] = (
        ok and [hc.hint for hc in got] == ["", "", ""])
    checks["on_host sees exactly what is returned"] = ok and seen == got
    checks["on_host runs for a host before the next host is checked"] = (
        EVENTS == [("check_all", "local"), ("on_host", "local"),
                   ("check_all", "nas"), ("on_host", "nas"),
                   ("check_all", "pve"), ("on_host", "pve")])
    if not checks["on_host runs for a host before the next host is checked"]:
        print(f"      → {EVENTS}")
    checks["the debug sink is check_all's bot=, and only= is not passed"] = (
        all(x.calls == [{"bot": sink}] for x in ck.values()))
    if not checks["the debug sink is check_all's bot=, and only= is not passed"]:
        print(f"      → {[x.calls for x in ck.values()]}")

    # Without a sink the call is a bare check_all(), the Web UI's form.
    ck2 = {"local": Checker("local"), "nas": Checker("nas")}
    _walk([a, b], ck2)
    checks["no debug sink: check_all() with no arguments at all"] = (
        [x.calls for x in ck2.values()] == [[{}], [{}]])

    # ── only_for: each host its own names ────────────────────────────
    ck3 = {"local": Checker("local"), "nas": Checker("nas")}
    names = {"local": {"web", "webdav"}, "nas": {"web"}}
    _walk([a, b], ck3, debug_sink=sink, only_for=lambda h: names[h.name])
    checks["only_for gives each host's check_all its own only="] = (
        ck3["local"].calls == [{"bot": sink, "only": {"web", "webdav"}}]
        and ck3["nas"].calls == [{"bot": sink, "only": {"web"}}])

    # ── the hint ─────────────────────────────────────────────────────
    no_dir = m._NO_SSH_DIR
    real_home = hostdiag._home_ssh
    hostdiag._home_ssh = lambda: no_dir
    try:
        de = get_translator("de")
        ck4 = {"local": Checker("local"), "nas": Checker("nas", raises=boom),
               "pve": Checker("pve", raises=OSError("connection refused"))}
        got, _seen = _walk([a, b, c], ck4, hint=True, t=de)
        want = hostdiag.hint(b.endpoint, boom, t=de)
        ok = isinstance(got, list) and len(got) == 3
        checks["hint=True: an SSH host's failure carries hostdiag's hint"] = (
            ok and bool(want) and got[1].hint == want)
        checks["…in the reader's language"] = (
            ok and bool(want)
            and want == de("hostdiag_ssh_no_dir", dir=no_dir)
            and want != get_translator("en")("hostdiag_ssh_no_dir",
                                             dir=no_dir))
        checks["…and a host hostdiag has nothing for, or that answered, "
               "gets \"\""] = ok and got[0].hint == "" and got[2].hint == ""
        if not ok:
            print(f"      → got {got!r}")

        # A hint that throws costs the hint, never the check.
        real_hint = hostdiag.hint

        def _hint_raises(*a, **k):
            raise RuntimeError("the hint blew up")
        hostdiag.hint = _hint_raises
        try:
            got, _seen = _walk([a, b, c], ck4, hint=True)
        finally:
            hostdiag.hint = real_hint
        ok = isinstance(got, list) and len(got) == 3
        checks["a hint that throws leaves the failure without one"] = (
            ok and got[1].error is boom and got[1].hint == "")
        checks["…and the walk still reaches the next host"] = (
            ok and got[2].error is not None)
    finally:
        hostdiag._home_ssh = real_home

    # ── one host: today's rule, the exception goes up ────────────────
    for label, targets in (("None", None), ("[None]", [None]), ("[]", [])):
        solo_boom = RuntimeError("daemon gone")
        cks = {None: Checker("solo", raises=solo_boom)}
        got, seen = _walk(targets, cks)
        checks[f"single host (targets={label}): the exception goes up"] = (
            isinstance(got, _Raised) and got.exc is solo_boom and not seen)
        # Discord never raised out of /check, not even alone (W6d).
        got, seen = _walk(targets, cks, raise_alone=False)
        checks[f"…unless raise_alone=False (targets={label}): reported, "
               f"for the pseudo-host None"] = (
            isinstance(got, list) and len(got) == 1 and got[0].host is None
            and got[0].error is solo_boom and got[0].updates is None
            and seen == got)
    cks = {None: Checker("solo", found)}
    got, seen = _walk(None, cks, debug_sink=sink)
    checks["single host: one answer, for the pseudo-host None"] = (
        isinstance(got, list) and len(got) == 1 and got[0].host is None
        and got[0].updates is found and cks[None].calls == [{"bot": sink}])
    # One managed host named on a multi-host install (`/check @nas`) is
    # not a single-host install: reported, not raised.
    got, seen = _walk([b], {"nas": Checker("nas", raises=boom)})
    checks["one host of several, named alone: reported, not raised"] = (
        isinstance(got, list) and len(got) == 1 and got[0].error is boom)

    # ── what is the caller's stays the caller's ──────────────────────
    def _on_host_raises(hc):
        raise LookupError("caller bug")
    ck5 = {"local": Checker("local"), "nas": Checker("nas")}
    got, _seen = _walk([a, b], ck5, on_host=_on_host_raises)
    checks["an exception from on_host is not swallowed"] = (
        isinstance(got, _Raised) and isinstance(got.exc, LookupError)
        and ck5["nas"].calls == [])

    def _checker_for_raises(h):
        if h.name == "nas":
            raise KeyError("no checker")
        return ck5[h.name]
    got = _try(check_on_hosts, [a, b, c], checker_for=_checker_for_raises,
               t=get_translator("en"))
    checks["a host without a checker is that host's failure"] = (
        isinstance(got, list) and len(got) == 3
        and isinstance(got[1].error, KeyError) and got[1].checker is None)
    got, _seen = _walk([a], {"local": Checker("local")}, on_host=None)
    checks["on_host is optional"] = isinstance(got, list) and len(got) == 1

    # ── W7: a read skips a remote host found dead a moment ago ───────
    # `use_cache=True` is what Telegram's /check, /check --dry-run and
    # /check <name> pass. A remote host still in its cooldown is not
    # asked — no checker, no check_all, no hint — and comes back as a
    # failure whose words are the remembered ones plus how long until
    # the next try. A host that could not LIST its containers (a
    # timeout, ContainerListUnavailable) is remembered; one whose check
    # failed for any other reason is not, because its daemon answered
    # and a /status a moment later must still ask it. A host that
    # checked fine is forgotten. Without use_cache (/update, Discord and
    # the Web until W7d/W7w) the memory is neither read nor written. The
    # local host is never skipped or marked. hosts' clock is replaced so
    # the seconds do not depend on this machine's speed.
    import subprocess
    import hosts as hostslib
    from update_checker import ContainerListUnavailable

    _clock = [5000.0]

    class _Time:
        @staticmethod
        def monotonic():
            return _clock[0]

    def _mem(name):
        left, why = hostslib.unreachable_for(name)
        return (round(left, 3), why)

    en = get_translator("en")
    timeout = subprocess.TimeoutExpired(["docker", "ps"], 30)
    _real_time = hostslib.time
    hostslib.time = _Time
    try:
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("nas", "no answer earlier", cooldown=30.5)
        hostslib.mark_unreachable("local", "local once", cooldown=30.5)
        ck7 = {"local": Checker("local"), "nas": Checker("nas"),
               "pve": Checker("pve")}
        asked = []

        def _checker_for7(h):
            asked.append(h.name)
            return ck7[h.name]
        del EVENTS[:]
        seen7 = []
        got = _try(check_on_hosts, [a, b, c], checker_for=_checker_for7,
                   hint=True, use_cache=True, t=en,
                   on_host=lambda hc: seen7.append(hc))
        ok = isinstance(got, list) and len(got) == 3
        checks["W7 cache: a remembered remote host is not asked: no "
               "checker, no check_all"] = (
            ok and "nas" not in asked and ck7["nas"].calls == [])
        skipped = got[1] if ok else None
        checks["W7 cache: …it is that host's failure, in its place"] = (
            ok and skipped.host is b and skipped.updates is None
            and skipped.error is not None and skipped.checker is None
            and [hc.host for hc in seen7] == [a, b, c])
        checks["W7 cache: …its words are the remembered ones and the "
               "seconds left, rounded up"] = (
            ok and str(skipped.error) == en(
                "host_skipped_retry", error="no answer earlier", seconds=31))
        checks["W7 cache: …and no hint is asked for a host nobody asked"] = (
            ok and skipped.hint == "")
        checks["W7 cache: the local host is checked even when it is in the "
               "memory, and its entry is left alone"] = (
            ok and len(ck7["local"].calls) == 1
            and _mem("local") == (30.5, "local once"))

        # What is remembered after a walk, per kind of failure.
        hostslib.forget_unreachable()
        ck7 = {"local": Checker("local"),
               "nas": Checker("nas", raises=timeout),
               "pve": Checker("pve", raises=ContainerListUnavailable(
                   "could not list containers (rc=255): ssh: refused")),
               "flaky": Checker("flaky", raises=RuntimeError(
                   "registry did not answer in 15s"))}
        fl = Host("flaky")
        got = _try(check_on_hosts, [a, b, c, fl],
                   checker_for=lambda h: ck7[h.name], use_cache=True, t=en)
        checks["W7 cache: a host whose check timed out is remembered, with "
               "the words a reader got"] = (
            _mem("nas") == (60.0, "no answer within 30 seconds"))
        checks["W7 cache: a host that could not list its containers is "
               "remembered"] = (
            _mem("pve") == (60.0, "could not list containers (rc=255): "
                                  "ssh: refused"))
        checks["W7 cache: a host whose daemon answered but whose check "
               "failed is NOT remembered"] = _mem("flaky") == (0.0, "")
        checks["W7 cache: …and the local host is never marked"] = (
            _mem("local") == (0.0, ""))

        # Answering forgets, backoff included: past nas' first minute it
        # checks fine, and its next failure is a first one (60 s) again.
        _clock[0] += 61
        ck7["nas"] = Checker("nas")
        got = _try(check_on_hosts, [b], checker_for=lambda h: ck7[h.name],
                   use_cache=True, t=en)
        checks["W7 cache: past its cooldown the host is checked again"] = (
            len(ck7["nas"].calls) == 1)
        hostslib.mark_unreachable("nas", "x")
        checks["W7 cache: …and checking fine reset its backoff"] = (
            _mem("nas")[0] == 60.0)

        # Without use_cache nothing is read or written.
        hostslib.forget_unreachable()
        hostslib.mark_unreachable("nas", "no answer earlier", cooldown=30.5)
        ck7 = {"local": Checker("local"), "nas": Checker("nas"),
               "pve": Checker("pve", raises=timeout)}
        got = _try(check_on_hosts, [a, b, c],
                   checker_for=lambda h: ck7[h.name], t=en)
        checks["W7 no cache: a remembered host is checked anyway"] = (
            len(ck7["nas"].calls) == 1)
        checks["W7 no cache: …and nothing is written"] = (
            _mem("nas") == (30.5, "no answer earlier")
            and _mem("pve") == (0.0, ""))

        # A single-host install has no name to remember: it raises as
        # before, cache or not.
        hostslib.forget_unreachable()
        got = _try(check_on_hosts, None,
                   checker_for=lambda h: Checker("solo", raises=timeout),
                   use_cache=True, t=en)
        checks["W7 cache: a single-host install still raises"] = (
            isinstance(got, _Raised) and got.exc is timeout)

        # Telegram says a skipped host's failure through errfmt.human,
        # which turns a line ENDING in "timed out after N seconds" into a
        # bare timeout. Remembered words often end like that (the
        # overview and the Web page remember the CLI's raw line), so the
        # skipped line says them the way errfmt.human would have, and in
        # every language the seconds clause comes after them. The first
        # version kept the raw words: after a /status, /check then quoted
        # Python's argv at the reader, where a fresh /check said "no
        # answer within 30 seconds".
        import errfmt
        import i18n
        raw = ("Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' "
               "timed out after 10 seconds")
        refused = "ssh: connect to host dead port 22: Connection refused"
        langs = sorted(f[:-5] for f in os.listdir(
            os.path.join(os.path.dirname(cf.__file__), "lang"))
            if f.endswith(".json"))
        kept = {}
        for lang in langs:
            tr = i18n.get_translator(lang)
            words = hostslib.skipped_words(raw, 42, t=tr)
            other = hostslib.skipped_words(refused, 42, t=tr)
            kept[lang] = (errfmt.human(hostslib.StillUnreachable(words), tr)
                          == words
                          and words.startswith(errfmt.human(raw, tr))
                          and "Command '" not in words and "42" in words
                          and words != tr("host_skipped_retry")
                          and other.startswith(refused))
        checks[f"W7: in all {len(langs)} languages a skipped host's line "
               f"says its words as errfmt.human does, then its seconds, "
               f"and survives errfmt.human"] = (
            len(langs) == 16 and all(kept.values()))
        if not all(kept.values()):
            print(f"      → {[k for k, v in kept.items() if not v]}")
    finally:
        hostslib.time = _real_time
        hostslib.forget_unreachable()


# ═════════════════════════════════════════════════════════════════════
# Telegram, through the real dispatcher on T2's hosts
# ═════════════════════════════════════════════════════════════════════
m._TMP_ROOT = tempfile.mkdtemp(prefix="check-on-hosts-")
calls, sinks = [], []
real_walk = check_on_hosts


def recording(targets, **k):
    calls.append(([getattr(h, "name", h) for h in targets]
                  if targets is not None else None, dict(k)))
    sinks.append(type(k.get("debug_sink")).__name__)
    return real_walk(targets, **k)


def row(setup, text, *, raw=False, refused=None):
    """T2's record for one typed command, with every check_on_hosts call
    in `calls`. `raw` leaves long messages unclipped."""
    del calls[:]
    del sinks[:]
    real_short = m._short
    real_refused = m.MatrixBackend.refused_stderr
    if raw:
        m._short = str
    if refused is not None:
        m.MatrixBackend.refused_stderr = refused
    if callable(real_walk):
        cf.check_on_hosts = recording
    try:
        return m.run_row(setup, text)
    finally:
        m._short = real_short
        m.MatrixBackend.refused_stderr = real_refused
        if callable(real_walk):
            cf.check_on_hosts = real_walk


ALL = ["local", "nas", "dead", "refused", "flaky"]
ANSWERED = ["local", "nas", "flaky"]       # dead/refused drop out at lookup
SITES = [
    ("multi", "/check", ALL, True),
    ("single", "/check", None, True),
    ("multi", "/check web @nas", ["nas"], False),
    ("multi", "/check --dry-run", ALL, False),
    ("multi", "/dryrun", ALL, False),
    ("single", "/dryrun", None, False),
    ("multi", "/check web", ANSWERED, False),
    ("multi", "/check web*", ANSWERED, False),
    ("single", "/check web", [None], False),
    ("multi", "/update web @all", ANSWERED, False),
    ("multi", "/update web", ["local"], False),
]
try:
    for setup, text, hosts_walked, hint in SITES:
        rec, esc = row(setup, text)
        want = m.EXPECTED.get((setup, text)) or m.EXPECTED.get(
            ("single", text))
        checks[f"Telegram {text} ({setup}) still answers as its T2 row"] = (
            rec == want and not esc)
        if rec != want:
            print(f"      → got {rec!r}")
        one = len(calls) == 1
        checks[f"…and asks check_on_hosts once, on {hosts_walked}, "
               f"hint={hint}, the bot as debug sink ({text}, {setup})"] = (
            one and calls[0][0] == hosts_walked
            and calls[0][1].get("hint", False) is hint
            and sinks == ["TelegramBot"])
        if not one:
            print(f"      → check_on_hosts calls: {calls}")
        # W7: a read takes the dead-host memory, /update never does.
        read = not text.startswith("/update")
        checks[f"…with use_cache={read} ({text}, {setup})"] = (
            one and calls[0][1].get("use_cache", False) is read)

    # Where the hint shows. hostdiag is the stand-in here, so the check
    # sees whether a site ASKS for one, whatever hostdiag would say.
    real_hint = hostdiag.hint
    hostdiag.hint = lambda *a, **k: "HINT-MARKER"
    try:
        rec, _e = row("multi", "/check", raw=True)
        failed = [o for o in rec["out"] if "could not be checked" in o]
        checks["/check puts the hint under every host that failed its check"] = (
            len(failed) == 3
            and all(o.endswith("\n\nHINT-MARKER") for o in failed))
        for setup, text in (("multi", "/check --dry-run"),
                            ("multi", "/dryrun"), ("multi", "/check web"),
                            ("multi", "/check web*"),
                            ("multi", "/update web @all")):
            rec, _e = row(setup, text, raw=True)
            failed = [o for o in rec["out"] if "could not be checked" in o]
            checks[f"{text} says a failed check without a hint"] = (
                bool(failed) and not any("HINT-MARKER" in o for o in failed))
    finally:
        hostdiag.hint = real_hint

    # …and the real hint, end to end, under the real failure.
    no_dir = m._NO_SSH_DIR
    real_home = hostdiag._home_ssh
    hostdiag._home_ssh = lambda: no_dir
    try:
        denied = "user@192.0.2.30: Permission denied (publickey)."
        rec, esc = row("multi", "/check", raw=True, refused=denied)
    finally:
        hostdiag._home_ssh = real_home
    hint_en = get_translator("en")("hostdiag_ssh_no_dir", dir=no_dir)
    line = [o for o in rec["out"]
            if o.startswith("⚠️ Host `refused` could not be checked: ")]
    checks["/check: a refused SSH host's failure ends in hostdiag's hint"] = (
        len(line) == 1 and denied in line[0]
        and line[0].endswith("\n\n" + hint_en) and not esc)
    if len(line) != 1 or not line[0].endswith(hint_en):
        print(f"      → {line!r}")

    # ═════════════════════════════════════════════════════════════════
    # Discord, through the real _on_event on T4's hosts (W6d)
    # ═════════════════════════════════════════════════════════════════
    # Discord's /check walked its hosts in a loop of its own: never
    # raised, said a failure as `host_check_failed` with the error cut to
    # 80 characters, no hint, `check_all()` with no arguments. Each row
    # is held to its T4 entry, byte for byte, and the walk to
    # check_on_hosts asked once, without a hint, a sink or a scope, and
    # told not to raise on a single-host install.
    import test_discord_dispatch_matrix as d
    steps_of = dict(d.all_scenarios())

    def dc_row(setup, name):
        del calls[:]
        del sinks[:]
        if callable(real_walk):
            cf.check_on_hosts = recording
        try:
            return d.run_scenario(setup, steps_of[name])
        finally:
            if callable(real_walk):
                cf.check_on_hosts = real_walk

    DC_SITES = [
        ("multi", "/check", [ALL]),
        ("one", "/check", [["local"]]),
        ("multi", "/check host:nas", [["nas"]]),
        ("multi", "/check host:all", [ALL]),
        ("multi", "/check host:dead", [["dead"]]),
        ("multi", "/check host:refused", [["refused"]]),
        ("multi", "/check host:flaky", [["flaky"]]),
        ("multi", "/check twice", [ALL, ALL]),
        ("multi", "lock: /check", []),          # the busy gate comes first
        ("one", "lock: /check", []),
    ]
    for setup, name, walks in DC_SITES:
        rec, esc = dc_row(setup, name)
        want = d.EXPECTED.get((setup, name))
        checks[f"Discord {name} ({setup}) still answers as its T4 row"] = (
            want is not None and rec == want and not esc)
        if rec != want:
            print(f"      → got {rec!r}")
        asked = [c[0] for c in calls]
        quiet = all(not c[1].get("hint", False)
                    and c[1].get("debug_sink") is None
                    and c[1].get("only_for") is None
                    and c[1].get("raise_alone", True) is False
                    for c in calls)
        checks[f"…and asks check_on_hosts {len(walks)}× on {walks}, no "
               f"hint, sink or scope, never raising ({name}, {setup})"] = (
            asked == walks and quiet)
        if asked != walks or not quiet:
            print(f"      → check_on_hosts calls: {calls}")

    # No registry at all (`hosts=None`, the constructor's default): the
    # walk is the single pseudo-host, and Discord still says a failed
    # check instead of raising out of the handler.
    import types
    from discord_bot import DiscordBot

    class _SoloChecker:
        def __init__(self, result=None, raises=None):
            self.result, self.raises, self.calls = result, raises, []

        def check_all(self, **kw):
            self.calls.append(dict(kw))
            if self.raises is not None:
                raise self.raises
            return self.result

    def _solo(checker):
        cfg = types.SimpleNamespace(discord_bot_token="t",
                                    discord_app_id="app",
                                    discord_guild_id="g", pending_file="",
                                    discord_allowed_users=[], debug=False)
        bot = DiscordBot(cfg, None, types.SimpleNamespace(
            update_running=False), checker=checker, log=lambda *_: None)
        del calls[:]
        if callable(real_walk):
            cf.check_on_hosts = recording
        try:
            return _try(bot._cmd_check, {})
        finally:
            if callable(real_walk):
                cf.check_on_hosts = real_walk

    long_err = "daemon gone: " + "x" * 120
    solo = _SoloChecker(raises=RuntimeError(long_err))
    out = _solo(solo)
    want = ("**Updates available**\n⚠️ Host `local` could not be checked: "
            + long_err[:80])
    checks["Discord without a registry: a failed check is a line, not a "
           "raise"] = out == want
    if out != want:
        print(f"      → got {out!r}")
    checks["…and that walk is check_on_hosts on the pseudo-host, told "
           "not to raise"] = (
        [c[0] for c in calls] == [[None]]
        and [c[1].get("raise_alone", True) for c in calls] == [False])
    solo = _SoloChecker([{"name": "web", "image": "nginx:1.25"}])
    out = _solo(solo)
    checks["Discord without a registry: an update is listed without a "
           "host tag"] = (
        out == "**Updates available**\n• `web` — nginx:1.25"
        and solo.calls == [{}])
    if out != "**Updates available**\n• `web` — nginx:1.25":
        print(f"      → got {out!r}")

    # A bot built without a checker (the constructor's default; main.py
    # always passes one). The old loop skipped such a host, so the answer
    # was "up to date" with nothing checked. Pinned as measured on the
    # tree before W6d, not as what it should say.
    out = _solo(None)
    up_to_date = get_translator("en")("chan_all_up_to_date")
    checks["Discord without a checker: still \"up to date\", as before "
           "W6d"] = out == up_to_date
    if out != up_to_date:
        print(f"      → got {out!r}")
    checks["…and no walk is started for it"] = calls == []

    # ═════════════════════════════════════════════════════════════════
    # Web, through the real do_GET on T5's hosts (W6w)
    # ═════════════════════════════════════════════════════════════════
    # The Web UI's "Check Updates" (GET /api/check) walked
    # hosts.host_checkers in a loop of its own: `check_all()` with no
    # arguments (no `bot=`, so DEBUG does not push the trace to Telegram
    # for a click), a raise printed as "Web UI check error on <host>"
    # with the name only on a multi-host install, everything found handed
    # to notify_updates once at the end. Each row is held to its T5
    # entry, byte for byte, and the walk to check_on_hosts asked as often
    # as the row clicks past the two busy gates, without a hint, a sink
    # or a scope, and told not to raise.
    import test_web_action_matrix as w
    web_steps = dict(w.all_scenarios())

    def web_row(setup, name):
        del calls[:]
        del sinks[:]
        if callable(real_walk):
            cf.check_on_hosts = recording
        try:
            return w.run_scenario(setup, web_steps[name])
        finally:
            if callable(real_walk):
                cf.check_on_hosts = real_walk

    WEB_SITES = [
        ("multi", "GET /api/check", [ALL]),
        ("one", "GET /api/check", [["local"]]),
        ("headless", "GET /api/check", [["local"]]),
        ("multi", "GET /api/check twice", [ALL, ALL]),
        ("one", "GET /api/check twice", [["local"], ["local"]]),
        ("multi", "GET /api/check: cross-origin", [ALL]),
        ("multi", "GET /api/check: update lock held", []),
        ("one", "GET /api/check: update lock held", []),
        ("multi", "GET /api/check: check lock held", []),
        ("headless", "GET /api/check: check lock held", []),
        ("multi", "gate: GET /api/check, no credentials", []),
    ]
    for setup, name, walks in WEB_SITES:
        rec, esc = web_row(setup, name)
        want = w.EXPECTED.get((setup, name))
        checks[f"Web {name} ({setup}) still answers as its T5 row"] = (
            want is not None and rec == want and not esc)
        if rec != want:
            print(f"      → got {rec!r}")
        asked = [c[0] for c in calls]
        quiet = all(not c[1].get("hint", False)
                    and c[1].get("debug_sink") is None
                    and c[1].get("only_for") is None
                    and c[1].get("raise_alone", True) is False
                    for c in calls)
        checks[f"…and asks check_on_hosts {len(walks)}× on {walks}, no "
               f"hint, sink or scope, never raising ({name}, {setup})"] = (
            asked == walks and quiet)
        if asked != walks or not quiet:
            print(f"      → check_on_hosts calls: {calls}")

    # Straight onto the handler, the way test_manual_check_hosts builds
    # it: no registry (`hosts=None`), a one-host and a two-host registry.
    # What a click leaves behind is the log line and what reaches
    # notify_updates; both are read off here.
    import contextlib
    import io
    import web_ui
    from hosts import HostRegistry, ManagedHost

    def _click(hosts, checker):
        notified = []
        bot = types.SimpleNamespace(update_running=False,
                                    notify_updates=notified.extend)
        handler_cls = web_ui.create_handler(
            types.SimpleNamespace(), checker=checker, bot=bot, store=None,
            backend=object(), hosts=hosts)
        h = handler_cls.__new__(handler_cls)
        del calls[:]
        out = io.StringIO()
        if callable(real_walk):
            cf.check_on_hosts = recording
        try:
            with contextlib.redirect_stdout(out):
                raised = _try(h._api_check)
        finally:
            if callable(real_walk):
                cf.check_on_hosts = real_walk
        locked = web_ui._CHECK_LOCK.locked()
        if locked:
            web_ui._CHECK_LOCK.release()
        return {"log": out.getvalue(), "notified": notified,
                "raised": isinstance(raised, _Raised), "locked": locked}

    def _reg(*pairs):
        return HostRegistry([ManagedHost(n, None, c, None,
                                         is_local=(i == 0))
                             for i, (n, c) in enumerate(pairs)])

    upd = {"name": "web", "image": "nginx:1.25"}
    solo = _SoloChecker(raises=RuntimeError("daemon gone"))
    got = _click(None, solo)
    checks["Web without a registry: a failed check is one log line "
           "without a host name, nothing notified"] = got == {
        "log": "Web UI check error: daemon gone\n", "notified": [],
        "raised": False, "locked": False}
    if got["log"] != "Web UI check error: daemon gone\n":
        print(f"      → got {got!r}")
    checks["…through one check_on_hosts walk, told not to raise"] = (
        len(calls) == 1 and calls[0][1].get("raise_alone", True) is False)

    solo = _SoloChecker([upd])
    got = _click(None, solo)
    checks["Web without a registry: what it found is notified, "
           "check_all asked with no arguments"] = (
        got == {"log": "", "notified": [upd], "raised": False,
                "locked": False} and solo.calls == [{}])

    one = _SoloChecker(raises=RuntimeError("boom"))
    got = _click(_reg(("local", one)), _SoloChecker([upd]))
    checks["Web on one host: the registry's checker is asked, the error "
           "line has no \"on local\""] = got == {
        "log": "Web UI check error: boom\n", "notified": [],
        "raised": False, "locked": False}
    if got["log"] != "Web UI check error: boom\n":
        print(f"      → got {got!r}")

    a = _SoloChecker(raises=RuntimeError("boom"))
    b = _SoloChecker([upd])
    got = _click(_reg(("local", a), ("nas", b)), a)
    checks["Web on two hosts: the failing one is named, the other's "
           "update still notified"] = got == {
        "log": "Web UI check error on local: boom\n", "notified": [upd],
        "raised": False, "locked": False} and b.calls == [{}]
    if got["log"] != "Web UI check error on local: boom\n":
        print(f"      → got {got!r}")
    a = _SoloChecker([dict(upd, host="local")])
    b = _SoloChecker([dict(upd, host="nas")])
    got = _click(_reg(("local", a), ("nas", b)), a)
    checks["Web on two hosts: both hosts' updates in one "
           "notify_updates, local first"] = (
        got["notified"] == [dict(upd, host="local"), dict(upd, host="nas")]
        and got["log"] == "")
finally:
    shutil.rmtree(m._TMP_ROOT, ignore_errors=True)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print(f"{len(checks) - len(failed)}/{len(checks)}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
