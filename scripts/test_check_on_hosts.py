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
finally:
    shutil.rmtree(m._TMP_ROOT, ignore_errors=True)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print(f"{len(checks) - len(failed)}/{len(checks)}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
