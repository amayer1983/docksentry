#!/usr/bin/env python3
"""`/check` answers per host, as each one finishes (#2, @famewolf).

Three hosts, one bot, and his observation after the first real run:

    You currently wait until it's checked all hosts before responding.
    You may want to consider responding per host at least for providing
    info.

Measured, it was half true — a host WITH updates already answered the
moment it finished. But a host that was up to date said nothing until
every host was done, so the first feedback sat on the slowest machine's
SSH round-trip. With one box asleep behind a slow link, "Checking…"
was followed by a minute of nothing.

Now every host produces exactly one thing when it completes: its
updates, its "everything up to date", or its failure. A single-host
install keeps its original single summary untouched — the docstring of
`_run_full_check` promises byte-identical messages there, and this
holds it to that.

The walk itself is `container_flags.check_on_hosts` since W6 (its own
procedure is test_check_on_hosts); `_run_full_check` hands it an
`on_host` that sends as each host finishes. The rows below drive the
real thing through the bot, and one check says the bot goes through
the core rather than a loop of its own.
"""

import os
import sys
import types

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_flags  # noqa: E402
import telegram_bot  # noqa: E402

checks = {}


class Checker:
    def __init__(self, updates=None, fail=False):
        self._u = updates or []
        self._fail = fail

    def check_all(self, bot=None):
        if self._fail:
            raise OSError("ssh: connect to host nas port 22: timed out")
        return list(self._u)

    def has_selfupdate_available(self):
        return False


def bot(hosts):
    b = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    b.sent = []
    b.send_message = lambda text, **kw: b.sent.append(text)
    b.notify_updates = lambda u: b.sent.append(f"UPDATES:{u[0]['name']}")
    b.notify_no_updates = lambda: b.sent.append("AGGREGATE-UPTODATE")
    b.t = lambda key, **kw: key + (":" + kw["host"] if "host" in kw else "")
    b._multi = lambda: len(hosts) > 1
    b._checker_for = lambda host, fallback: (host.checker if host else fallback)
    b.config = types.SimpleNamespace(debug=False)
    return b


def run_full_check(b, checker, hosts):
    """`b._run_full_check(...)`, or a FAIL line if it has moved."""
    run = srcslice.attr(b, "_run_full_check",
                        label="TelegramBot._run_full_check")
    if run is None:
        return False
    run(checker, hosts)
    return True


class Host:
    def __init__(self, name, checker):
        self.name = name
        self.checker = checker
        self.endpoint = f"ssh://{name}"
        self.is_local = name == "local"


# ── three hosts: one with updates, one clean, one dead ───────────────
hosts = [Host("local", Checker(updates=[{"name": "web"}])),
         Host("nas", Checker()),
         Host("pve", Checker(fail=True))]
b = bot(hosts)
walks = []
_real_walk = getattr(container_flags, "check_on_hosts", None)


def _recording(targets, **kw):
    walks.append((targets, kw.get("hint"), kw.get("debug_sink")))
    return _real_walk(targets, **kw)


if callable(_real_walk):
    container_flags.check_on_hosts = _recording
try:
    ran = run_full_check(b, hosts[0].checker, hosts)
finally:
    if callable(_real_walk):
        container_flags.check_on_hosts = _real_walk
checks["/check walks its hosts through container_flags.check_on_hosts"] = (
    len(walks) == 1 and walks[0][0] is hosts)
checks["…with the hint, and the bot as the debug sink"] = (
    len(walks) == 1 and walks[0][1] is True and walks[0][2] is b)

checks["the host with updates answers"] = "UPDATES:web" in b.sent
checks["the clean host says so itself, at once"] = any(
    "host_check_uptodate:nas" in m for m in b.sent)
checks["the dead host reports its failure"] = any(
    "host_check_failed" in m for m in b.sent)
checks["…and no aggregate line repeats what each host already said"] = (
    ran and "AGGREGATE-UPTODATE" not in b.sent)

# Order: each host's line appears in walk order — the point is that the
# fast ones are not held behind the slow one.
idx = {m: i for i, m in enumerate(b.sent)}
_nas = next((m for m in b.sent if "uptodate:nas" in m), None)
checks["answers arrive in completion order"] = (
    "UPDATES:web" in idx and _nas is not None
    and idx["UPDATES:web"] < idx[_nas])

# ── every host clean: three lines, no fourth summary ─────────────────
hosts2 = [Host("local", Checker()), Host("nas", Checker())]
b2 = bot(hosts2)
ran2 = run_full_check(b2, hosts2[0].checker, hosts2)
checks["all-clean multi-host: one line per host"] = (
    sum("host_check_uptodate" in m for m in b2.sent) == 2)
checks["…and still no aggregate"] = (
    ran2 and "AGGREGATE-UPTODATE" not in b2.sent)

# ── a single-host install is untouched, as the docstring promises ────
solo = [Host("local", Checker())]
b3 = bot(solo)
b3._multi = lambda: False
ran3 = run_full_check(b3, solo[0].checker, None)
checks["single host keeps the original aggregate"] = (
    "AGGREGATE-UPTODATE" in b3.sent)
checks["…and no per-host line it never had"] = ran3 and not any(
    "host_check_uptodate" in m for m in b3.sent)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
