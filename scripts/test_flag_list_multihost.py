#!/usr/bin/env python3
"""Listing a flag across hosts shows what is set, not a wall of "none".

`/pin dsentry-web @srv30` then `/pin` reported "no pinned containers" and
the local-host-only hint — because the LIST mode of a flag command
inherited the WRITE default (local), while a listing is a read and should
span every host like `/status`. Found live on the multi-host bench: the
pin was on srv30, the store proved it, and `/pin` said there were none.

Two halves. The targeting — list mode resolves to every host — is a
front-end concern (`_flag_targets` in Telegram, `_hosts_for` in Discord).
This pins down the CORE half: `apply_flag`/`set_cooldown` in list mode
across several hosts must show only the hosts that have entries, and say
"none" exactly once, only when nothing is set anywhere. A pin on one host
must not come buried under four other hosts' "none"s. And a single-host
install must still get its one host-less line, byte-for-byte.
"""
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import container_flags as cf  # noqa: E402

checks = {}


class Store:
    def __init__(self, pinned=()):
        self._p = list(pinned)

    def get_pinned(self):
        return list(self._p)


class Host:
    def __init__(self, name, local=False):
        self.name = name
        self.is_local = local


PIN = cf.FLAGS["pin"]
_be = lambda h: None  # noqa: E731  — list mode never touches the backend


def listing(targets, stores):
    return cf.apply_flag(PIN, targets, store_for=lambda h: stores[h],
                         backend_for=_be, partial=None)


# ── multi-host: entries on ONE host only ─────────────────────────────
a, b, c = Host("local", local=True), Host("srv30"), Host("srv40")
stores = {a: Store(), b: Store(["dsentry-web"]), c: Store()}
out = listing([a, b, c], stores)
keys = [r.key for r in out.replies]
checks["only the host with entries is listed"] = keys == ["pin_list"]
checks["…and it is the right host"] = out.replies[0].host is b
checks["…with the entry in it"] = "dsentry-web" in out.replies[0].items
checks["empty hosts produce no line at all"] = (
    "pin_empty" not in keys and len(out.replies) == 1)

# ── multi-host: nothing set anywhere → exactly one "none" ────────────
stores = {a: Store(), b: Store(), c: Store()}
out = listing([a, b, c], stores)
checks["nothing anywhere says 'none' exactly once"] = (
    [r.key for r in out.replies] == ["pin_empty"])
checks["…host-less (not attributed to one of several)"] = (
    out.replies[0].host is None)

# ── entries on TWO hosts → both listed, no empties ───────────────────
stores = {a: Store(["x"]), b: Store(["y"]), c: Store()}
out = listing([a, b, c], stores)
checks["several hosts with entries are all listed"] = (
    [r.key for r in out.replies] == ["pin_list", "pin_list"]
    and {r.host for r in out.replies} == {a, b})

# ── single host still gets its one host-less line ────────────────────
# `[None]` is the pseudo-host the single-host callers pass.
out = listing([None], {None: Store()})
checks["single host, empty: one host-less 'none' line"] = (
    [r.key for r in out.replies] == ["pin_empty"]
    and out.replies[0].host is None)
out = listing([None], {None: Store(["web"])})
checks["single host, set: one host-less list line"] = (
    [r.key for r in out.replies] == ["pin_list"]
    and out.replies[0].host is None
    and "web" in out.replies[0].items)

# ── set_cooldown list mode follows the same rule ─────────────────────
class CdStore:
    def __init__(self, cds=None):
        self._c = dict(cds or {})

    def get_cooldowns(self):
        return dict(self._c)


cd = {a: CdStore(), b: CdStore({"dsentry-web": 60}), c: CdStore()}
out = cf.set_cooldown([a, b, c], store_for=lambda h: cd[h],
                      backend_for=_be, partial=None, seconds=None)
checks["cooldown list: only the host with one is shown"] = (
    [r.key for r in out.replies] == ["cooldown_list"]
    and out.replies[0].host is b)
cd = {a: CdStore(), b: CdStore(), c: CdStore()}
out = cf.set_cooldown([a, b, c], store_for=lambda h: cd[h],
                      backend_for=_be, partial=None, seconds=None)
checks["cooldown list: one 'none' when none anywhere"] = (
    [r.key for r in out.replies] == ["cooldown_empty"]
    and out.replies[0].host is None)

# ── the front ends resolve a listing to every host ───────────────────
tb = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
checks["Telegram has a read/write-aware flag resolver"] = (
    "def _flag_targets" in tb and "write=not listing" in tb)
checks["…and the flag handlers use it"] = (
    tb.count("self._flag_targets(text)") >= 6)
db = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Discord lists across all hosts when no container is named"] = (
    db.count("self._hosts_for(opts.get(\"host\")) if not arg") >= 5)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
