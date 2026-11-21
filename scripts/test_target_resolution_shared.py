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
  * `_resolve_targets` keeps no private copy of the rule;
  * Discord's `_hosts_for` / `_write_hosts_for` give the core's targets
    on multi for every spelling of the `host` option, `all` included
    (A2: reads used to answer 'unknown host' to it), with None for an
    unknown host; on one host `_hosts_for` keeps its own path and `all`
    stays unknown there; autocomplete with `host: all` asks the local
    host only.

`--app DIR` runs it against another tree's app/ (that is how the
Telegram half was measured on main before the core function existed).
"""
import os
import sys
import types

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

# ── Discord: the same core, the `host` option as typed ────────────────
# Discord has no `@` to parse: the option holds a bare name, and `all`
# (any case) is the sentinel. `_hosts_for` answers None for "no such
# host", where the core answers ([], Reply).
import discord_bot                                 # noqa: E402


def dc_on(registry, checker=None):
    bot = discord_bot.DiscordBot.__new__(discord_bot.DiscordBot)
    bot.hosts = registry
    bot.checker = checker
    return bot


def names_of(targets):
    if not isinstance(targets, list):
        return targets
    return [getattr(h, "name", h) for h in targets]


#: The option value → the token the core gets.
DC_TOKEN = {None: None, "": None, "nas": "nas", "NAS": "NAS",
            " nas ": " nas ", "all": ALL_HOSTS, "ALL": ALL_HOSTS,
            " All ": ALL_HOSTS, "typo": "typo"}
EVERY = ["local", "nas", "pve1"]
#: Multi-host, by hand: what a read and a write reach (None = unknown).
DC_MULTI = {
    None: (EVERY, ["local"]), "": (EVERY, ["local"]),
    "nas": (["nas"], ["nas"]), "NAS": (["nas"], ["nas"]),
    " nas ": (["nas"], ["nas"]),
    "all": (EVERY, EVERY), "ALL": (EVERY, EVERY), " All ": (EVERY, EVERY),
    "typo": (None, None),
}
dc = dc_on(REGISTRIES["multi"])
for opt, (want_read, want_write) in DC_MULTI.items():
    for mode, fn, want in (("read", "_hosts_for", want_read),
                           ("write", "_write_hosts_for", want_write)):
        cell = f"multi/{mode}/host:{opt!r}"
        got = call(getattr(dc, fn, None), opt)
        checks[f"DC {cell} → {want}"] = names_of(got) == want
        core = call(by_mode[mode], REGISTRIES["multi"], DC_TOKEN[opt])
        same = (isinstance(core, tuple) and len(core) == 2
                and got == (None if core[1] is not None else core[0]))
        checks[f"DC {cell}: same targets as the core"] = same

# One host: `_hosts_for` keeps its own path — the registry's one host,
# not the core's `[None]` — and `all` stays an unknown host there, as
# today. Writes ignore the option on one host (the core's `[None]`).
DC_ONE_READ = {None: ["local"], "": ["local"], "local": ["local"],
               "LOCAL": ["local"], "nas": None, "all": None, "ALL": None,
               "zzz": None}
dc = dc_on(REGISTRIES["one"])
for opt, want in DC_ONE_READ.items():
    checks[f"DC one/read/host:{opt!r} → {want}"] = (
        names_of(call(dc._hosts_for, opt)) == want)
    checks[f"DC one/write/host:{opt!r} → [None]"] = (
        call(dc._write_hosts_for, opt) == [None])
dc = dc_on(None)
for opt in DC_ONE_READ:
    checks[f"DC none/read/host:{opt!r} → [None]"] = (
        call(dc._hosts_for, opt) == [None])

_dc_body = srcslice.method_source(discord_bot.DiscordBot, "_hosts_for")
checks["DC _hosts_for routes multi-host reads through "
       "container_flags.targets_for_read"] = (
    _dc_body is not None and "container_flags.targets_for_read(" in _dc_body)

# Autocomplete with `host: all` lists the LOCAL host's containers — it
# does not walk every host while someone is typing, and it does not take
# whichever host happens to come first. `build_hosts` always puts local
# first, so the second registry below is hand-built with local second:
# it pins that the local host is asked for, not the order relied on.


class _Backend:
    def __init__(self, names):
        self.names, self.calls = names, 0

    def run(self, args, **_kw):
        self.calls += 1
        return types.SimpleNamespace(stdout="\n".join(self.names) + "\n",
                                     returncode=0)


def _autocomplete(order, opt):
    local_be, nas_be = _Backend(["web", "db"]), _Backend(["plex"])
    hs = {"local": ManagedHost("local", local_be, None, None, is_local=True),
          "nas": ManagedHost("nas", nas_be, None, None)}
    bot = dc_on(HostRegistry([hs[n] for n in order]),
                checker=types.SimpleNamespace(backend=local_be))
    got = call(bot._container_names, opt)
    return got, (local_be.calls, nas_be.calls)


for order in (("local", "nas"), ("nas", "local")):
    for opt in ("all", "ALL", " all "):
        got, calls = _autocomplete(order, opt)
        checks[f"DC autocomplete host:{opt!r} (registry {'/'.join(order)}) "
               f"lists local only"] = got == ["web", "db"] and calls == (1, 0)
    got, calls = _autocomplete(order, "nas")
    checks[f"DC autocomplete host:'nas' (registry {'/'.join(order)}) "
           f"lists nas"] = got == ["plex"] and calls == (0, 1)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
