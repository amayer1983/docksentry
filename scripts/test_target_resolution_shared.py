#!/usr/bin/env python3
"""Which hosts a command acts on is decided once, in the core (#7).

The rule is one sentence: a command that only LOOKS defaults to every
host, one that CHANGES something stays on the local host unless it is
aimed. It lived twice — `container_flags.targets_for_write` (writes,
used by Discord) and `TelegramBot._resolve_targets`, which made the same
decision for both halves itself. Two copies of one rule drift; the
multi-host pass already shipped five bugs of that shape.

Now `container_flags.targets_for(mode, registry, token)` decides, with
`targets_for_read` / `targets_for_write` as the names the call sites
read, and Telegram only translates: it parses `@host` out of the text
and renders the fatal Reply in its own language.

What this pins, over tokens {None, nas, NAS, @all, typo} × {read, write}
× registry {none, one host, three hosts}, against a real `HostRegistry`:

  * the core's answer on every cell, written out by hand;
  * Telegram's `_resolve_targets` gives the same targets and the same
    error on multi — `None` instead of `[]` for "no targets", as the
    `targets or [None]` call sites expect;
  * on a single-host install `_resolve_targets` returns the argument
    text byte for byte (an `@nas` token and double spaces kept) and
    never parses it: that early return is what keeps one-host installs
    on their pre-#7 path;
  * `_resolve_targets` keeps no private copy of the rule.

`--app DIR` runs it against another tree's app/ (that is how the
Telegram half was measured on main before the core function existed).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _app_dir():
    if "--app" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--app") + 1])
    return os.path.join(ROOT, "app")


APP = _app_dir()
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import container_flags as cf                       # noqa: E402
import hosts                                       # noqa: E402
import srcslice                                    # noqa: E402
import telegram_bot                                # noqa: E402
from hosts import ALL_HOSTS, HostRegistry, ManagedHost   # noqa: E402

checks = {}


def host(name, is_local=False):
    return ManagedHost(name, None, None, None, is_local=is_local)


LOCAL, NAS, PVE = host("local", is_local=True), host("nas"), host("pve1")
REGISTRIES = {
    "none": None,
    "one": HostRegistry([host("local", is_local=True)]),
    "multi": HostRegistry([LOCAL, NAS, PVE]),
}
TOKENS = (None, "nas", "NAS", ALL_HOSTS, "typo")
MODES = ("read", "write")
UNKNOWN = ("host_unknown", {"name": "typo",
                            "hosts": "`local`, `nas`, `pve1`"})

#: The multi-host answer, by hand: (targets, fatal (key, params) or None).
EXPECTED_MULTI = {
    ("read", None): ([LOCAL, NAS, PVE], None),
    ("write", None): ([LOCAL], None),
    ("read", "nas"): ([NAS], None),
    ("write", "nas"): ([NAS], None),
    ("read", "NAS"): ([NAS], None),
    ("write", "NAS"): ([NAS], None),
    ("read", ALL_HOSTS): ([LOCAL, NAS, PVE], None),
    ("write", ALL_HOSTS): ([LOCAL, NAS, PVE], None),
    ("read", "typo"): ([], UNKNOWN),
    ("write", "typo"): ([], UNKNOWN),
}


def tok_label(tok):
    return {None: "no host", ALL_HOSTS: "@all"}.get(tok, f"@{tok}")


def call(fn, *args, **kw):
    """The result, or ('raised', repr) — a FAIL line, never a traceback."""
    if fn is None:
        return ("missing",)
    try:
        return fn(*args, **kw)
    except Exception as e:                          # noqa: BLE001
        return ("raised", repr(e))


def fatal_view(reply):
    if reply is None:
        return None
    if getattr(reply, "ok", True) is not False:
        return ("not marked ok=False", reply)
    return (reply.key, dict(reply.params))


def core_view(result):
    if not (isinstance(result, tuple) and len(result) == 2):
        return result
    targets, fatal = result
    return (list(targets) if isinstance(targets, list) else targets,
            fatal_view(fatal))


def render(key, **params):
    """Stand-in for TelegramBot.t: what was asked for, exactly."""
    return f"{key}|{sorted(params.items())}"


def bot_on(registry):
    bot = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    bot.hosts = registry
    bot.t = render
    return bot


read_fn = getattr(cf, "targets_for_read", None)
write_fn = getattr(cf, "targets_for_write", None)
shared_fn = getattr(cf, "targets_for", None)
by_mode = {"read": read_fn, "write": write_fn}
checks["container_flags.targets_for_read exists"] = read_fn is not None
checks["container_flags.targets_for exists"] = shared_fn is not None

# ── the core, every cell ──────────────────────────────────────────────
for setup, registry in REGISTRIES.items():
    for mode in MODES:
        for tok in TOKENS:
            cell = f"{setup}/{mode}/{tok_label(tok)}"
            got = core_view(call(by_mode[mode], registry, tok))
            if setup == "multi":
                want = EXPECTED_MULTI[(mode, tok)]
            else:
                # One host: nothing to route, every token walks the
                # pseudo-host — even a typo, as before.
                want = ([None], None)
            names = [getattr(h, "name", h) for h in want[0]]
            checks[f"core {cell} → {want[1][0] if want[1] else names}"] = (
                got == want)
            checks[f"core {cell}: targets_for(mode) answers the same"] = (
                core_view(call(shared_fn, mode, registry, tok)) == got)

checks["core: an unknown mode is refused, not read as a write"] = (
    call(shared_fn, "wirte", REGISTRIES["multi"], None)[0] == "raised")
_fresh = call(read_fn, REGISTRIES["multi"], None)
checks["core: a read hands back a list of its own, not the registry's"] = (
    isinstance(_fresh, tuple) and len(_fresh) == 2
    and isinstance(_fresh[0], list)
    and _fresh[0] is not REGISTRIES["multi"].hosts)

# ── Telegram on multi: same targets, same error ───────────────────────
TEXT = {None: "web", "nas": "web @nas", "NAS": "web @NAS",
        ALL_HOSTS: "web @all", "typo": "web @typo"}
bot = bot_on(REGISTRIES["multi"])
for mode in MODES:
    for tok in TOKENS:
        cell = f"multi/{mode}/{tok_label(tok)}"
        got = call(bot._resolve_targets, TEXT[tok], write=(mode == "write"))
        targets, fatal = EXPECTED_MULTI[(mode, tok)]
        want = ("web", targets or None,
                render(fatal[0], **fatal[1]) if fatal else None)
        checks[f"TG {cell}: matches the table"] = got == want
        core = call(by_mode[mode], REGISTRIES["multi"], tok)
        if isinstance(core, tuple) and len(core) == 2 and isinstance(got, tuple):
            c_targets, c_fatal = core
            same = (got[1] == (c_targets or None) and got[2] == (
                render(c_fatal.key, **c_fatal.params) if c_fatal else None))
        else:
            same = False
        checks[f"TG {cell}: same (targets, error) as the core"] = same

# ── Telegram on one host: untouched, never parsed ─────────────────────
parsed = []
_real_split = hosts.split_host_target


def _counting_split(text):
    parsed.append(text)
    return _real_split(text)


RAW = ("web  @nas", "  web @NAS  ", "web\t@all", "web  @typo ", "web", "")
hosts.split_host_target = _counting_split
try:
    for setup in ("none", "one"):
        bot = bot_on(REGISTRIES[setup])
        for mode in MODES:
            for raw in RAW:
                got = call(bot._resolve_targets, raw, write=(mode == "write"))
                checks[f"TG {setup}/{mode} {raw!r}: text byte-identical, "
                       f"no targets, no error"] = (
                    isinstance(got, tuple) and len(got) == 3
                    and got[0] is raw and got[1] is None and got[2] is None)
finally:
    hosts.split_host_target = _real_split
checks["TG single host: the @-token is never parsed"] = parsed == []

# ── and Telegram keeps no private copy of the rule ────────────────────
body = srcslice.method_source(telegram_bot.TelegramBot, "_resolve_targets")
checks["TG _resolve_targets routes through container_flags.targets_for"] = (
    body is not None and "container_flags.targets_for(" in body)
checks["TG _resolve_targets does not resolve hosts itself"] = (
    body is not None and ".hosts.get(" not in body
    and "list(self.hosts)" not in body and "self.hosts.local" not in body
    and "ALL_HOSTS" not in body)
checks["TG _resolve_targets returns early on one host, before parsing"] = (
    srcslice.in_order(body, "if not self._multi():",
                      "return arg_text, None, None", "split_host_target("))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
