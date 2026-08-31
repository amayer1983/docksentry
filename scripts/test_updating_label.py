#!/usr/bin/env python3
"""While an update runs, the label says so — and to which version.

@LeeNX, #2: "I did not see any changes to the yellow `update` label, plus
I did get confused when the logs said updates were in progress, but the
label was still indicating there was an update, maybe the label could be
changed during the update progress to `updating to <version>` or
something."

The engine had no answer to give: `update_running` said an update was in
flight but never which container, so every front end could only repeat
"update available". This asserts the intent end to end:

  * the engine names what it is working on, for exactly as long as it
    works on it — including when the update fails;
  * the classic table and the V2 list both stop saying "available" and
    start saying "updating", with the target version when the image
    tells us one and without when it does not;
  * the wording comes out of app/lang/, not out of the source.

Deliberately not asserting the exact sentence: "updating to 1.26" is one
reasonable wording of many, and the translations are free to differ. What
must hold is that the running state is distinguishable from the waiting
one and carries the version.

No Docker, no sockets: a real UpdateEngine over a temp DATA_DIR with a
stub checker, and the Web UI handler built the way
scripts/test_web_selfupdate_row.py builds it.
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import i18n  # noqa: E402
import web_ui  # noqa: E402
import web_v2  # noqa: E402
from config import Config  # noqa: E402
from container_store import ContainerStore, host_key  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

MISSING = "/nonexistent/docksentry-test"
LANG_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "lang")

checks = {}


# ── 1. the engine can name what it is updating ───────────────────────
seen = []


class StubChecker:
    """Just enough of an UpdateChecker for one batch entry."""

    def __init__(self, engine, fail=False):
        self.engine = engine
        self.fail = fail

    def netns_target_name(self, name):
        return None

    def get_container_labels(self, name):
        return {}

    def label_bool(self, labels, key):
        return None

    def update_container(self, name, image, netns_name=None, **kw):
        # What the UI would see at the exact moment the pull/recreate
        # is under way. That is the whole question.
        seen.append(self.engine.updating)
        if self.fail:
            raise RuntimeError("pull failed")
        return True, "updated"


with tempfile.TemporaryDirectory() as tmp:
    os.environ["DATA_DIR"] = tmp
    config = Config.from_env()
    os.environ.pop("DATA_DIR", None)
    engine = UpdateEngine(config, ContainerStore(config))

    checks["a fresh engine is updating nothing"] = engine.updating == {}

    engine._process_update_batch(
        [{"name": "nginx", "image": "nginx:1.25",
          "new_version": "1.26", "host": "local"}],
        StubChecker(engine), auto=False)
    checks["mid-update the engine names the container"] = (
        list(seen[-1]) == ["nginx"])
    checks["…and the version it is heading for"] = (
        seen[-1].get("nginx") == "1.26")
    checks["…and lets go when it is done"] = engine.updating == {}

    # No OCI version label on the remote image → no target. Must not
    # invent one, must still report the container as busy.
    seen.clear()
    engine._process_update_batch(
        [{"name": "redis", "image": "redis:7", "host": "local"}],
        StubChecker(engine), auto=False)
    checks["an unknown target is empty, not guessed"] = (
        seen[-1] == {"redis": ""})

    # A failed update must clear too — a badge stuck on "updating…"
    # after a rollback is the same wrong answer the other way round.
    seen.clear()
    try:
        engine._process_update_batch(
            [{"name": "redis", "image": "redis:7", "host": "local"}],
            StubChecker(engine, fail=True), auto=False)
    except Exception:
        pass
    checks["a failed update lets go as well"] = engine.updating == {}

    # Two hosts can each run an `nginx`; the claim must not collide.
    engine.mark_updating(host_key("nas", "nginx"), "1.26")
    checks["claims are host-scoped"] = (
        "nginx" not in engine.updating and "nas/nginx" in engine.updating)
    engine.clear_updating(host_key("nas", "nginx"))

    # The lock is still the thing that says "busy" — this did not
    # replace it with a second opinion.
    checks["the lock still answers 'is anything running?'"] = (
        engine.update_running is False)

    engine.mark_updating("nginx", "1.26")
    snapshot = engine.updating
    snapshot["nginx"] = "tampered"
    checks["callers get a copy, not the live dict"] = (
        engine.updating["nginx"] == "1.26")
    engine.clear_updating("nginx")


# ── 2. the classic table stops saying "available" ────────────────────
class FakeStore:
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


class FakeChecker:
    def _own_container_name(self): return "docksentry"
    def get_disk_usage(self): return 42.0, 1, 2
    def read_advisories(self): return {}


class FakeEngine:
    def __init__(self, updating): self.updating = dict(updating)


def render_status(updating, pending, language="en"):
    """The status page as a browser gets it."""
    cfg = types.SimpleNamespace(
        language=language, auto_selfupdate=False, ui_mode="advanced",
        debug=False, disk_warn_percent=85, status_view="table",
        pending_file=MISSING, history_file=MISSING, maintenance_file=MISSING)
    bot = types.SimpleNamespace(engine=FakeEngine(updating))
    cls = web_ui.create_handler(cfg, FakeChecker(), bot=bot, store=FakeStore())
    h = cls.__new__(cls)
    h.path = "/"
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._render_page = lambda content, active=None, wide=None: content
    h._get_containers = lambda: [
        {"name": "nginx", "image": "nginx:1.25", "health": "healthy",
         "labels": {}, "version": "", "short_id": "abc123456789"}]
    h._get_pending = lambda host=None: list(pending)
    h._page_status()
    return out.get("html", "")


PENDING = [{"name": "nginx", "image": "nginx:1.25",
            "new_version": "1.26", "host": "local"}]

waiting = render_status({}, PENDING)
running = render_status({"nginx": "1.26"}, PENDING)
unknown = render_status({"nginx": ""}, PENDING)

t_en = i18n.get_translator("en")
AVAILABLE = t_en("web_badge_update")

checks["waiting: the row says an update is available"] = (
    f">{AVAILABLE}</span>" in waiting)
checks["running: it stops saying that"] = (
    f">{AVAILABLE}</span>" not in running)
checks["running: it names the version instead"] = "1.26" in running
checks["running: waiting and running do not read the same"] = (
    waiting != running)
checks["running with no known target: still says something"] = (
    unknown != waiting and f">{AVAILABLE}</span>" not in unknown)
checks["…without printing a blank where the version would be"] = (
    web_ui._updating_label(t_en, "") in unknown
    and web_ui._updating_label(t_en, "1.26") not in unknown)

# The mobile card is built from the same locals as the table row, so it
# must have moved with it — that pair has drifted apart before.
checks["the narrow-screen card moved with the row"] = (
    running.count(web_ui._updating_label(t_en, "1.26")) >= 2)

# Offering "update now" beside a badge that says it is already updating
# just earns a second "already running".
checks["the row's update button is dead while it runs"] = (
    'action="/api/update"' in running and "disabled" in running)
checks["…and live while it only waits"] = (
    'action="/api/update"' in waiting)


# ── 3. the V2 list says the same thing ───────────────────────────────
view = {
    "host": "local",
    "containers": [{"name": "nginx", "image": "nginx:1.25",
                    "health": "healthy", "labels": {}}],
    "pending_names": ["nginx"],
    "updating": {"nginx": "1.26"},
    "pinned": [], "auto_list": [], "own_name": "docksentry",
    "groups": {}, "notes": {}, "links": {}, "advisories": {},
}
row = web_v2.container_rows(
    [view], host_key, lambda v: web_ui._updating_label(t_en, v))[0]
checks["V2: the row knows it is updating"] = row["updating"] is True
checks["V2: …and carries wording the client can print"] = (
    "1.26" in row["updating_label"])

quiet = dict(view, updating={})
checks["V2: an idle row is unchanged"] = (
    web_v2.container_rows([quiet], host_key)[0]["updating"] is False)

# The V2 client has its own hardcoded en/de label table; a new string
# that only lived there would never reach the other fourteen languages.
v2js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static",
                         "v2.js")).read()
checks["V2: the wording is not hardcoded in the client"] = (
    "updating_label" in v2js and "updating to" not in v2js)


# ── 4. the wording comes from app/lang/ ──────────────────────────────
KEYS = ("web_badge_updating", "web_badge_updating_now")
missing = []
for fn in sorted(os.listdir(LANG_DIR)):
    if not fn.endswith(".json"):
        continue
    with open(os.path.join(LANG_DIR, fn), encoding="utf-8") as fh:
        data = json.load(fh)
    for k in KEYS:
        if k not in data:
            missing.append(f"{fn}:{k}")
checks["every language file has the new keys"] = not missing
if missing:
    print("   missing:", ", ".join(missing))

checks["the version is a placeholder, not glued on"] = (
    "{version}" in json.load(
        open(os.path.join(LANG_DIR, "en.json"), encoding="utf-8")
    )["web_badge_updating"])

# Switching the language must move the badge, or it is not translated.
t_de = i18n.get_translator("de")
checks["a translated language words it differently"] = (
    web_ui._updating_label(t_de, "1.26")
    != web_ui._updating_label(t_en, "1.26"))
checks["…and still shows the version"] = (
    "1.26" in web_ui._updating_label(t_de, "1.26"))
checks["no target -> a wording without one"] = (
    web_ui._updating_label(t_en, "") != web_ui._updating_label(t_en, "1.26")
    and "1.26" not in web_ui._updating_label(t_en, ""))


for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
