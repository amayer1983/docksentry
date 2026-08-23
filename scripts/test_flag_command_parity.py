#!/usr/bin/env python3
"""/trustrunning, /askmajor and /cooldown behave the same in both chats.

These three were the last flag commands each front end still implemented
for itself, and they had drifted in both directions at once:

  * Telegram assembled `/trustrunning` and `/askmajor` by hand while
    Discord already went through the core — so a fix to the core reached
    one chat and not the other.
  * Telegram's `/cooldown` could list every cooldown that was set;
    Discord's required a container and a number, so the same question
    ("which of mine have one?") was answerable in one chat only.
  * Telegram parsed the seconds INSIDE the per-host loop, so `/cooldown
    web abc @all` answered for the first host and then aborted, having
    already written nothing but said something.

All three now run on `container_flags`, and this checks the behaviour
rather than the spelling.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import container_flags as cf  # noqa: E402

checks = {}


class Store:
    def __init__(self, trust=(), major=(), cooldowns=None):
        self._trust = list(trust)
        self._major = list(major)
        self._cd = dict(cooldowns or {})

    def get_trust_running(self):
        return list(self._trust)

    def toggle_trust_running(self, name):
        if name in self._trust:
            self._trust.remove(name)
            return False
        self._trust.append(name)
        return True

    def get_ask_before_major(self):
        return list(self._major)

    def toggle_ask_before_major(self, name):
        if name in self._major:
            self._major.remove(name)
            return False
        self._major.append(name)
        return True

    def get_cooldowns(self):
        return dict(self._cd)

    def get_cooldown(self, name):
        return self._cd.get(name, 0)

    def set_cooldown(self, name, secs):
        secs = max(0, min(600, int(secs)))     # the store's clamp
        if secs:
            self._cd[name] = secs
        else:
            self._cd.pop(name, None)
        return secs


class Backend:
    def run(self, argv, timeout=None):
        return types.SimpleNamespace(
            returncode=0, stdout="web\nplex\ndocksentry\n", stderr="")


store = Store()
be = Backend()
S = lambda h: store          # noqa: E731
B = lambda h: be             # noqa: E731

# ── the two toggles ──────────────────────────────────────────────────
for which, on_key, off_key, list_key, empty_key in (
        ("trustrunning", "trust_on", "trust_off", "trust_list", "trust_empty"),
        ("askmajor", "askmajor_on", "askmajor_off",
         "askmajor_list", "askmajor_empty")):
    spec = cf.FLAGS[which]
    o = cf.apply_flag(spec, [None], store_for=S, backend_for=B, partial=None)
    checks[f"{which}: empty list says so"] = (
        o.replies[0].key == empty_key)

    o = cf.apply_flag(spec, [None], store_for=S, backend_for=B, partial="we")
    checks[f"{which}: a partial name resolves"] = (
        o.replies[0].params.get("name") == "web")
    checks[f"{which}: turning it on says on"] = o.replies[0].key == on_key
    checks[f"{which}: …and it is recorded"] = o.changed

    o = cf.apply_flag(spec, [None], store_for=S, backend_for=B, partial="web")
    checks[f"{which}: toggling again says off"] = o.replies[0].key == off_key

    cf.apply_flag(spec, [None], store_for=S, backend_for=B, partial="web")
    o = cf.apply_flag(spec, [None], store_for=S, backend_for=B, partial=None)
    checks[f"{which}: a non-empty list is listed"] = (
        o.replies[0].key == list_key and "web" in o.replies[0].items)

    o = cf.apply_flag(spec, [None], store_for=S, backend_for=B,
                      partial="nosuch")
    checks[f"{which}: an unknown container is an error, not a write"] = (
        o.replies[0].ok is False and not o.changed)

# ── /cooldown ────────────────────────────────────────────────────────
store = Store()
o = cf.set_cooldown([None], store_for=S, backend_for=B, partial=None,
                    seconds=None)
checks["cooldown: nothing set → empty"] = o.replies[0].key == "cooldown_empty"

o = cf.set_cooldown([None], store_for=S, backend_for=B, partial="web",
                    seconds="45")
checks["cooldown: setting it reports the value"] = (
    o.replies[0].key == "cooldown_set"
    and o.replies[0].params["seconds"] == 45)

o = cf.set_cooldown([None], store_for=S, backend_for=B, partial="web",
                    seconds=None)
checks["cooldown: no seconds shows the current value"] = (
    o.replies[0].key == "cooldown_current"
    and o.replies[0].params["seconds"] == 45)

o = cf.set_cooldown([None], store_for=S, backend_for=B, partial=None,
                    seconds=None)
checks["cooldown: listing works with no container named"] = (
    o.replies[0].key == "cooldown_list")
checks["…and each entry carries its value"] = (
    cf.item_parts(o.replies[0].items[0]) == ("web", "45s"))

# The store clamps at 600 and reports what it actually stored, so 9999 is
# answered as 600 rather than as accepted.
o = cf.set_cooldown([None], store_for=S, backend_for=B, partial="web",
                    seconds="9999")
checks["cooldown: an over-large value is answered with the clamp"] = (
    o.replies[0].params["seconds"] == 600)

o = cf.set_cooldown([None], store_for=S, backend_for=B, partial="web",
                    seconds="0")
checks["cooldown: zero clears it"] = o.replies[0].key == "cooldown_cleared"

# ── the parse-before-you-write rule, across hosts ────────────────────
class Host:
    def __init__(self, name, local=False):
        self.name = name
        self.is_local = local

stores = {"a": Store(), "b": Store()}
hosts = [Host("a", local=True), Host("b")]
o = cf.set_cooldown(hosts, store_for=lambda h: stores[h.name],
                    backend_for=B, partial="web", seconds="abc")
checks["cooldown: a bad value is fatal, before any host is touched"] = (
    o.fatal is not None and o.fatal.key == "cooldown_bad_value")
checks["…and nothing was written on any host"] = (
    stores["a"].get_cooldowns() == {} and stores["b"].get_cooldowns() == {})

# A good value reaches every host, each answer tagged with its own.
o = cf.set_cooldown(hosts, store_for=lambda h: stores[h.name],
                    backend_for=B, partial="web", seconds="30")
checks["cooldown: @all writes on every host"] = (
    len(o.replies) == 2
    and stores["a"].get_cooldown("web") == 30
    and stores["b"].get_cooldown("web") == 30)
checks["…and each reply knows which host it is from"] = (
    [r.host.name for r in o.replies] == ["a", "b"])

# ── item_parts: both list shapes ─────────────────────────────────────
checks["a bare name has no detail"] = cf.item_parts("web") == ("web", "")
checks["a pair carries one"] = cf.item_parts(("web", "45s")) == ("web", "45s")

# ── and both front ends actually call the core ───────────────────────
tb = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
db = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
for cmd, key in (("/trustrunning", 'FLAGS["trustrunning"]'),
                 ("/askmajor", 'FLAGS["askmajor"]'),
                 ("/cooldown", "set_cooldown(")):
    i = tb.index(f'elif text.startswith("{cmd}")')
    body = tb[i:tb.index("\n        elif text", i + 10)]
    checks[f"Telegram {cmd} goes through the core"] = key in body
    # And nothing is assembled beside it: no store call, no hand-built
    # bullet line, no second copy of the resolve.
    checks[f"Telegram {cmd} keeps no logic of its own"] = (
        "self._store_for(host)" not in body
        and "self._resolve_container(" not in body)

checks["Discord /cooldown can list too"] = (
    'partial=arg or None' in db
    and '{"name": "container", "description": "Container to configure",\n'
        '          "type": 3, "required": False}' in db)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
