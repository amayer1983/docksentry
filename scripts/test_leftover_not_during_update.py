#!/usr/bin/env python3
"""The rollback copy of a running update is not called litter.

Mid-update both containers exist: the new one is already up, and `<name>_old`
is still the copy a rollback would restore from. The status banner counted
that as "left behind from an interrupted update" and offered a `docker rm`
for it — which, followed, removes the only thing a failed update could fall
back to. It cleared itself once the update finished, so it looked like a
harmless glitch rather than the advice it was.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

checks = {}

src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "web_ui.py")).read()

# The gathering loop, isolated by the two lines that bound it.
start = src.index('leftovers = []')
block = src[start:src.index("leftovers = sorted(set(leftovers))", start)]

checks["a leftover is only counted when its live twin exists"] = (
    'n[:-4] in _live' in block)
checks["…and not while that container is being updated"] = (
    'not in _busy' in block)
checks["the busy set comes from the engine, not a second bookkeeping"] = (
    '_updating_now(' in block)

# The engine is the one source: same call the badge uses. Slicing the
# haystack to START at the needle made this true by construction, so
# count instead — the badge and the leftover guard, and nothing that
# keeps its own second list.
checks["the badge reads the same place"] = (
    src.count("self._updating_now") >= 2
    and "_updating_here = self._updating_now" in src)

# A stale suffix check would match `foo_older`; make sure it is exact.
checks["only the exact _old suffix counts"] = ('endswith("_old")' in block)

# ── "nothing here removes them for you", driven ─────────────────────
# This was `'docker rm' in src and 'leftover' in src`, which the comment
# above the gathering loop satisfies by itself: measured in the 2.19.1
# audit, the `<pre>docker rm …</pre>` line deleted and this stayed green.
# A removal added to the page would never have shown up in it at all.
#
# So the page is rendered for real, with only the daemon stood in for: a
# DockerBackend whose `run()` records every argv and keeps its own list
# of containers, from which an `rm` really removes them. Then the same
# for `cleanup_images`. Its callers, listed from a grep of app/ and
# each opened: Telegram's `cleanup_guarded` (behind /cleanup, the Web UI
# button, the scheduler's auto-cleanup and its disk-warning cleanup),
# Discord's `_cleanup_guarded`, and the two in-batch cleanups in
# update_engine.py. It is the only `image_prune` caller, and the likeliest
# place for someone to add "and the _old containers too".
#
# What this does NOT cover, said plainly: a removal added in some other
# code path — the scheduler loop, a new command — would not be seen
# here. `recovery.py` is the one place that does remove an `_old`, its
# own in-flight one, and test_leftover_backups.py holds it to that.
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

import container_backend  # noqa: E402
import web_ui  # noqa: E402
from i18n import get_translator  # noqa: E402
from update_checker import UpdateChecker  # noqa: E402

TOUCHING = ("rm", "stop", "kill", "rename", "start", "restart",
            "container", "system", "create", "run")


class Daemon(container_backend.DockerBackend):
    def __init__(self, names):
        self.names = list(names)
        self.calls = []

    def run(self, args, **kw):
        args = list(args)
        self.calls.append(args)
        if args[:1] == ["ps"] and "-a" in args:
            return subprocess.CompletedProcess(
                args, 0, "\n".join(self.names) + "\n", "")
        if args[:1] == ["rm"]:
            self.names = [n for n in self.names if n not in args[1:]]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["image", "prune"]:
            return subprocess.CompletedProcess(
                args, 0, "Total reclaimed space: 0B\n", "")
        return subprocess.CompletedProcess(args, 1, "", "stand-in: refused")


class _Store:
    def get_pinned(self): return []
    def get_autoupdate(self): return []
    def get_ask_before_major(self): return []
    def get_groups(self): return {}
    def get_notes(self): return {}
    def get_links(self): return {}
    def get_pending_major(self): return {}
    def is_protect_stop(self, name): return False
    def is_trust_running(self, name): return False
    def get_cooldown(self, name): return 0


class _Checker:
    def _own_container_name(self): return "docksentry"
    def get_disk_usage(self): return 42.0, 1, 2
    def read_advisories(self): return {}


def render_status(daemon, updating=None):
    missing = "/nonexistent/docksentry-test"
    cfg = types.SimpleNamespace(
        language="en", auto_selfupdate=False, ui_mode="advanced",
        debug=False, disk_warn_percent=85, status_view="table",
        pending_file=missing, history_file=missing, maintenance_file=missing)
    bot = types.SimpleNamespace(
        engine=types.SimpleNamespace(updating=dict(updating or {})))
    cls = web_ui.create_handler(cfg, _Checker(), bot=bot, store=_Store(),
                                backend=daemon)
    h = cls.__new__(cls)
    h.path = "/"
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._render_page = lambda content, active=None, wide=None: content
    h._get_containers = lambda: [
        {"name": "nginx", "image": "nginx:1.25", "health": "healthy",
         "labels": {}, "version": "", "short_id": "abc123456789"}]
    h._get_pending = lambda host=None: []
    h._page_status()
    return out.get("html", "")


TITLE = get_translator("en")("web_leftover_title")
daemon = Daemon(["nginx", "nginx_old", "stranger_old"])
page = render_status(daemon)
checks["the Status page names the leftover backup"] = (
    TITLE in page and "nginx_old" in page)
checks["…offers the command to remove it, for the user to run"] = (
    "docker rm nginx_old" in page)
checks["…and leaves out an _old whose live twin does not exist"] = (
    "stranger_old" not in page)
checks["rendering it sends the daemon nothing that changes a container"] = (
    not [c for c in daemon.calls if c[:1] and c[0] in TOUCHING])
page2 = render_status(daemon)
checks["…so on the next load the backup is still there to be named"] = (
    "nginx_old" in daemon.names and "docker rm nginx_old" in page2)

# Counter-directions. Mid-update the `_old` is the rollback copy and not
# named; and the stand-in really does honour an `rm`, so "still there"
# above is not something it could only ever answer one way.
checks["while nginx is updating, its _old is not called leftover"] = (
    TITLE not in render_status(daemon, updating={"nginx": "1.26"}))
daemon.rm(["nginx_old"], force=True)
checks["once the user has removed it, the notice is gone"] = (
    TITLE not in render_status(daemon))

# Every cleanup path runs `cleanup_images`; it prunes images and must
# leave the leftover backups to the user as well.
_d = tempfile.mkdtemp()
_cfg = types.SimpleNamespace(
    debug=False, language="en", cleanup_grace_hours=24,
    cleanup_backup_local_only=False, data_dir=_d,
    cleanup_backup_dir=os.path.join(_d, "backups"), cleanup_backup_days=7)
cdaemon = Daemon(["nginx", "nginx_old"])
_ok, _msg = UpdateChecker(_cfg, backend=cdaemon).cleanup_images()
checks["a cleanup run prunes images (it did run)"] = (
    _ok is True and any(c[:2] == ["image", "prune"] for c in cdaemon.calls))
checks["…and removes no container, the leftover backup included"] = (
    "nginx_old" in cdaemon.names
    and not [c for c in cdaemon.calls if c[:1] and c[0] in TOUCHING])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
