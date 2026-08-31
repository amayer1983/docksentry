#!/usr/bin/env python3
"""Per-host container state is actually WIRED UP (#7).

`test_host_scoped_store.py` proves the `HostScopedStore` class keys state
per host. This one proves the thing that was missing: that the real
consumers — the Telegram command handlers, `UpdateEngine`, the pending
file and `UpdateChecker` — go through it. The class existed and was built
per host in `hosts.build_hosts`, but nothing in production ever read
`ManagedHost.store`, so pinning `nginx`, toggling its auto-update or
setting its cooldown did it for EVERY host at once, and one host's
auto-update pass deleted the other hosts' pending entries.

Two things are asserted throughout:

  * with several hosts managed, every piece of state belongs to exactly
    one of them;
  * with ONE host managed — the overwhelming majority of installs — every
    byte written and every reply sent is what it was before, and the keys
    on disk stay unprefixed. That is checked by running the identical
    command sequence through a bot with no registry, a bot with a
    one-host registry and a bot with a two-host registry, then diffing
    the resulting state files against each other and against the literal
    expected JSON.

No Telegram, no Docker, no network. Exits non-zero on any failure.
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from container_store import ContainerStore, HostScopedStore, LOCAL_HOST  # noqa: E402
from hosts import HostRegistry, ManagedHost                              # noqa: E402
from telegram_bot import TelegramBot                                     # noqa: E402
from update_checker import UpdateChecker                                 # noqa: E402
from update_engine import UpdateEngine                                   # noqa: E402

checks = {}

STATE_FILES = ["pinned", "autoupdate", "update_windows", "ask_before_major",
               "trust_running", "cooldown", "protect_stop", "major_pending",
               "groups", "notes", "links"]


class _CP:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class FakeBackend:
    """Answers `ps` for a fixed container list; everything else is empty."""

    def __init__(self, name, names):
        self.name = name
        self.names = list(names)

    def run(self, args, **kw):
        if args and args[0] == "ps":
            return _CP("\n".join(self.names))
        return _CP("")


class FakeChecker:
    """Enough of an UpdateChecker for the update engine to run dry."""

    def __init__(self, name):
        self.name = name
        self.updated = []

    def netns_target_name(self, name):
        return None

    def get_container_labels(self, name):
        return {}

    def label_bool(self, labels, key):
        return None

    def update_container(self, name, image, **kw):
        self.updated.append((self.name, name))
        return True, "ok"


def make_cfg(d):
    cfg = types.SimpleNamespace(
        # `announce` reads `bot.enabled`, which reads these two — a fake
        # config without them is a config no real one can be (#61).
        bot_token="", chat_id="",
        language="en", debug=False, container_cli="docker",
        auto_update_all=False, update_policy="all",
        data_dir=d,
        pending_file=os.path.join(d, "pending_updates.json"),
        history_file=os.path.join(d, "update_history.json"),
    )
    for n in STATE_FILES:
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return cfg


def make_bot(d, host_names=()):
    """A real TelegramBot over a real ContainerStore in `d`.

    `host_names=()` builds it with NO registry (a pre-#7 embedder and the
    single-host default); `("local",)` builds a one-host registry;
    `("local", "nas")` a two-host one. Returns (bot, registry, checkers).
    """
    cfg = make_cfg(d)
    store = ContainerStore(cfg)
    hosts, checkers = None, {}
    local_backend = FakeBackend(LOCAL_HOST, ["web", "db"])
    if host_names:
        managed = []
        for name in host_names:
            is_local = name == LOCAL_HOST
            backend = local_backend if is_local else FakeBackend(name, ["web", "plex"])
            checker = FakeChecker(name)
            checkers[name] = checker
            managed.append(ManagedHost(name, backend, checker,
                                       HostScopedStore(store, name),
                                       endpoint="" if is_local else f"ssh://{name}",
                                       is_local=is_local))
        hosts = HostRegistry(managed)
    bot = TelegramBot(cfg, store, hosts=hosts)
    bot.backend = local_backend
    bot.start_time = 0
    bot.bot_username = "dockbot"
    bot.sent = []
    bot.send_message = lambda text, reply_markup=None, auto=False: bot.sent.append(text)
    bot._check_auth = lambda *a, **k: True
    bot._enrich_with_source_url = lambda ups: None
    if not checkers:
        checkers[LOCAL_HOST] = FakeChecker(LOCAL_HOST)
    return bot, store, checkers


def drive(bot, cmd, checker=None):
    bot.sent = []
    bot._handle_message({"text": cmd, "from": {"id": 1}, "chat": {"id": 1}},
                        checker, None)
    return list(bot.sent)


def state_bytes(d):
    """Every state file in `d` as raw bytes, keyed by file name."""
    out = {}
    for n in STATE_FILES:
        p = os.path.join(d, f"{n}.json")
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[n] = f.read()
    return out


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


# ── 1. bot command paths write to the targeted host only ──────────────
with tempfile.TemporaryDirectory() as d:
    bot, store, checkers = make_bot(d, ("local", "nas"))
    local_view = HostScopedStore(store, LOCAL_HOST)
    nas_view = HostScopedStore(store, "nas")

    drive(bot, "/pin web @nas")
    checks["pin @nas pins on nas"] = nas_view.get_pinned() == ["web"]
    checks["pin @nas does NOT pin on local"] = local_view.get_pinned() == []
    checks["pin @nas is stored host-keyed"] = store.get_pinned() == ["nas/web"]

    drive(bot, "/pin web")
    checks["a second pin on local leaves nas' pin intact"] = (
        local_view.get_pinned() == ["web"] and nas_view.get_pinned() == ["web"])
    checks["both pins coexist on disk"] = sorted(store.get_pinned()) == [
        "nas/web", "web"]

    drive(bot, "/unpin web")
    checks["unpin on local leaves nas pinned"] = (
        local_view.get_pinned() == [] and nas_view.get_pinned() == ["web"])

    # auto-update toggle
    drive(bot, "/autoupdate web @nas")
    checks["auto-update toggle is per host"] = (
        nas_view.get_autoupdate() == ["web"] and local_view.get_autoupdate() == [])
    drive(bot, "/autoupdate web")
    checks["toggling local auto-update leaves nas on"] = (
        local_view.get_autoupdate() == ["web"] and nas_view.get_autoupdate() == ["web"])
    off = drive(bot, "/autoupdate web @nas")
    checks["toggling nas off leaves local on"] = (
        local_view.get_autoupdate() == ["web"] and nas_view.get_autoupdate() == [])
    checks["per-host toggle reply names the host"] = any("@nas" in t for t in off)

    # cooldown
    drive(bot, "/cooldown web 20 @nas")
    checks["cooldown is per host"] = (
        nas_view.get_cooldown("web") == 20 and local_view.get_cooldown("web") == 0)
    drive(bot, "/cooldown web 45")
    checks["local cooldown doesn't disturb nas'"] = (
        local_view.get_cooldown("web") == 45 and nas_view.get_cooldown("web") == 20)

    # stop-protection
    drive(bot, "/protect web @nas")
    checks["protect-stop is per host"] = (
        nas_view.is_protect_stop("web") and not local_view.is_protect_stop("web"))

    # link override
    drive(bot, "/setlink web https://nas.example/changelog @nas")
    checks["link override is per host"] = (
        nas_view.get_link("web") == "https://nas.example/changelog"
        and local_view.get_link("web") == "")

    # …and the notification link resolver reads it back for the same host,
    # so a per-host override actually reaches the per-host notification.
    ups = [{"name": "web", "image": "reg/web:1", "host": "nas"},
           {"name": "web", "image": "reg/web:1", "host": "local"}]
    bot.engine.link_resolver.label_link = lambda name, checker=None: ""
    bot.engine.link_resolver.container_source_url = lambda name, checker=None: ("", "none")
    bot.engine.link_resolver.enrich_with_source_url(ups)
    checks["link resolver picks the entry's own host override"] = (
        ups[0]["source_url"] == "https://nas.example/changelog"
        and ups[1]["source_url"] != "https://nas.example/changelog")

    # unknown host still refuses to touch anything
    before = store.get_pinned()
    out = drive(bot, "/pin web @typo")
    checks["unknown host errors and writes nothing"] = (
        len(out) == 1 and "typo" in out[0] and store.get_pinned() == before)


# ── 2. the update engine resolves state per update host ───────────────
with tempfile.TemporaryDirectory() as d:
    bot, store, checkers = make_bot(d, ("local", "nas"))
    local_view = HostScopedStore(store, LOCAL_HOST)
    nas_view = HostScopedStore(store, "nas")
    engine = bot.engine

    # A cooldown set on nas must not stall a local recreate, and vice versa.
    nas_view.set_cooldown("web", 30)
    import time as _time_mod
    slept = []
    orig_sleep = _time_mod.sleep
    _time_mod.sleep = lambda s: slept.append(s)
    try:
        slept.clear()
        engine._maybe_cooldown("web", more_remaining=True, host=LOCAL_HOST)
        checks["engine: local recreate ignores nas' cooldown"] = slept == []
        slept.clear()
        engine._maybe_cooldown("web", more_remaining=True, host="nas")
        checks["engine: nas recreate uses nas' cooldown"] = slept == [30]
    finally:
        _time_mod.sleep = orig_sleep

    nas_view.set_cooldown("web", 0)      # keep the batches below instant

    # ask-before-major is read from the update's own host.
    nas_view.toggle_ask_before_major("web")
    checks["engine: ask-major list is the update's host's"] = (
        "web" in nas_view.get_ask_before_major()
        and "web" not in local_view.get_ask_before_major())

    # groups belong to a host: a local group must not reorder nas' batch.
    local_view.save_group("g", "G", ["db", "web"], wait_seconds=0)
    updates = [{"name": "web", "image": "i", "host": "nas"},
               {"name": "db", "image": "i", "host": "nas"}]
    engine._process_update_batch(list(updates), checkers["nas"], auto=False)
    checks["engine: a local group doesn't order another host's batch"] = (
        [n for _h, n in checkers["nas"].updated] == ["web", "db"])
    local_updates = [{"name": "web", "image": "i", "host": "local"},
                     {"name": "db", "image": "i", "host": "local"}]
    engine._process_update_batch(local_updates, checkers["local"], auto=False)
    checks["engine: the local group DOES order the local batch"] = (
        [n for _h, n in checkers["local"].updated] == ["db", "web"])


# ── 3. the pending file is per (host, container) ──────────────────────
PENDING_BOTH = [
    {"name": "web", "image": "reg/web:1", "host": "local"},
    {"name": "web", "image": "reg/web:2", "host": "nas"},
    {"name": "plex", "image": "reg/plex:1", "host": "nas"},
]

with tempfile.TemporaryDirectory() as d:
    bot, store, checkers = make_bot(d, ("local", "nas"))
    with open(bot.config.pending_file, "w") as f:
        json.dump(PENDING_BOTH, f)
    # nas auto-updates its `web`; local's identically-named entry and nas'
    # own `plex` (not in this batch) must both survive.
    HostScopedStore(store, "nas").toggle_auto("web")
    bot.handle_autoupdates([u for u in PENDING_BOTH if u["host"] == "nas"],
                           checkers["nas"])
    left = read_json(bot.config.pending_file, [])
    checks["auto pass: only nas' web was updated"] = (
        checkers["nas"].updated == [("nas", "web")])
    checks["auto pass on nas keeps local's pending entry"] = (
        {"name": "web", "image": "reg/web:1", "host": "local"} in left)
    checks["auto pass on nas keeps nas' other pending entry"] = any(
        e["name"] == "plex" for e in left)
    checks["auto pass on nas dropped exactly its own processed entry"] = (
        len(left) == 2
        and not any(e["name"] == "web" and e["host"] == "nas" for e in left))

with tempfile.TemporaryDirectory() as d:
    bot, store, checkers = make_bot(d, ("local", "nas"))
    with open(bot.config.pending_file, "w") as f:
        json.dump(PENDING_BOTH, f)
    bot._run_single_update(checkers["local"], "nas/web")
    left = read_json(bot.config.pending_file, [])
    checks["single update: the host key picked nas' checker"] = (
        checkers["nas"].updated == [("nas", "web")]
        and checkers["local"].updated == [])
    checks["single update on nas leaves local's same-named entry"] = (
        [(e["name"], e["host"]) for e in left]
        == [("web", "local"), ("plex", "nas")])

with tempfile.TemporaryDirectory() as d:
    bot, store, checkers = make_bot(d, ("local", "nas"))
    with open(bot.config.pending_file, "w") as f:
        json.dump(PENDING_BOTH, f)
    bot._remove_from_pending([("nas", "web")])
    left = read_json(bot.config.pending_file, [])
    checks["removing (nas, web) leaves (local, web)"] = (
        [(e["name"], e["host"]) for e in left]
        == [("web", "local"), ("plex", "nas")])
    # A pre-#7 entry carries no `host` key at all and must read as local.
    with open(bot.config.pending_file, "w") as f:
        json.dump([{"name": "web", "image": "i"},
                   {"name": "web", "image": "i", "host": "nas"}], f)
    bot._remove_from_pending([("nas", "web")])
    left = read_json(bot.config.pending_file, [])
    checks["a host-less legacy entry is treated as local"] = (
        len(left) == 1 and "host" not in left[0])


# ── 4. UpdateChecker._get_pinned resolves for ITS OWN host ────────────
with tempfile.TemporaryDirectory() as d:
    cfg = make_cfg(d)
    with open(cfg.pinned_file, "w") as f:
        json.dump(["web", "nas/db"], f)
    local_checker = UpdateChecker(cfg, backend=FakeBackend(LOCAL_HOST, []))
    nas_checker = UpdateChecker(cfg, backend=FakeBackend("nas", []))
    checks["checker: local sees only unprefixed pins"] = (
        local_checker._get_pinned() == ["web"])
    checks["checker: nas sees its own pins, unprefixed"] = (
        nas_checker._get_pinned() == ["db"])
    # A backend with no `name` at all (the plain local Docker/Podman one)
    # is the local host — that's the single-host case.
    plain = UpdateChecker(cfg, backend=types.SimpleNamespace(
        run=lambda *a, **k: _CP("")))
    checks["checker: a nameless backend is the local host"] = (
        plain._get_pinned() == ["web"])


# ── 5. single-host installs are unchanged, byte for byte ──────────────
# The same command sequence through three different wirings. (a) no
# registry at all, (b) a one-host registry, (c) a two-host registry with
# every command defaulting to local. All three must leave identical state
# files, and those files must hold plain unprefixed keys.
SEQUENCE = ["/pin web", "/autoupdate web", "/cooldown web 20",
            "/protect web", "/setlink web https://example.com/changelog",
            "/pin", "/autoupdate", "/cooldown", "/protect"]

runs = {}
for label, host_names in (("no-registry", ()),
                          ("one-host", ("local",)),
                          ("two-host", ("local", "nas"))):
    d = tempfile.mkdtemp()
    bot, store, checkers = make_bot(d, host_names)
    replies = [drive(bot, cmd) for cmd in SEQUENCE]
    runs[label] = (state_bytes(d), replies)

checks["single host: a one-host registry writes the same bytes as none"] = (
    runs["no-registry"][0] == runs["one-host"][0])
checks["single host: a one-host registry sends the same replies as none"] = (
    runs["no-registry"][1] == runs["one-host"][1])
checks["adding a second host doesn't change what local writes"] = (
    runs["no-registry"][0] == runs["two-host"][0])

files = runs["no-registry"][0]
checks["single host: pinned file is the plain unprefixed list"] = (
    files.get("pinned") == b'["web"]')
checks["single host: autoupdate file is the plain unprefixed list"] = (
    files.get("autoupdate") == b'["web"]')
checks["single host: cooldown file is the plain unprefixed dict"] = (
    files.get("cooldown") == b'{\n  "web": 20\n}')
checks["single host: protect file is the plain unprefixed list"] = (
    files.get("protect_stop") == b'["web"]')
checks["single host: links file is the plain unprefixed dict"] = (
    files.get("links") == b'{\n  "web": "https://example.com/changelog"\n}')
checks["single host: no `/` ever reaches a state key"] = not any(
    b"/web" in blob for blob in files.values())
checks["single host: no reply carries a host marker or hint"] = not any(
    (" @" in t or "Local host only" in t)
    for replies in runs["no-registry"][1] for t in replies)


# ── 6. the auto path's pending write is unchanged for one host ────────
# The old code wrote `[u for u in updates if not processed]` over the whole
# file. With one host that is exactly what the host-aware merge produces.
for label, host_names in (("no-registry", ()), ("one-host", ("local",))):
    with tempfile.TemporaryDirectory() as d:
        bot, store, checkers = make_bot(d, host_names)
        updates = [{"name": "web", "image": "reg/web:1"},
                   {"name": "db", "image": "reg/db:1"}]
        with open(bot.config.pending_file, "w") as f:
            json.dump(updates, f)
        store.toggle_auto("web")
        bot.handle_autoupdates([dict(u) for u in updates], checkers[LOCAL_HOST])
        left = read_json(bot.config.pending_file, [])
        checks[f"single host ({label}): auto pass leaves exactly the untouched entry"] = (
            [(e["name"], e.get("host")) for e in left] == [("db", None)])


def main():
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
