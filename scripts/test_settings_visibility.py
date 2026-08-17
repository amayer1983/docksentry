#!/usr/bin/env python3
"""What you set, and what you can see (#2, @famewolf + @NotRetarded).

Two people spent four hours on 16 August chasing a setting that was
switched on and not on the page. It was auto-cleanup at 85% on three
hosts, visible on one. @famewolf found it himself in the end:

    Found it! It's a simple/advanced mode display issue. The setting WAS
    enabled but was not showing because the other two were in simple
    mode. I'd say that's a problem. If I have things enabled but can't
    see them in the gui how would I know to change them?

He is right. Simple mode is meant to be fewer knobs, not "your server is
doing things you cannot see". Hiding an option nobody has touched is
fine; hiding one that is on and acting is not.

Then, on the way out of that, he hit the other half three times in one
night:

    Env override: DISK_WARN_AUTO_CLEANUP=true is set in the environment,
    but the saved setting disk_warn_auto_cleanup=false wins
    […] I would think environment variables should have priority?

The precedence stays — flipping it would silently reset everyone who set
something in the env once and later changed it here. What was missing is
a way back that is not "edit settings.json inside the volume by hand".
"""

import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import settings_notices  # noqa: E402

checks = {}

# ═══ reading "hidden" out of the page, not out of a list ═════════════
# A hand-kept list of advanced keys is wrong the first time a block
# moves. The rule the CSS applies is "an ancestor carries adv-only", so
# that is the rule this reads back.
PAGE = """
<div class="card">
  <div class="grid">
    <input type="text" name="cron_schedule" value="0 18 * * *">
  </div>
  <div class="grid adv-only">
    <input type="number" name="disk_warn_percent" value="60">
    <div>
      <div class="form-checkbox-row">
        <input type="checkbox" name="disk_warn_auto_cleanup" checked>
      </div>
    </div>
  </div>
  <hr class="section-divider adv-only">
  <select name="language"><option>en</option></select>
  <div class="adv-only"><textarea name="registry_mirrors"></textarea></div>
</div>
"""

hidden = settings_notices.hidden_fields(PAGE)
checks["a field inside an adv-only block counts as hidden"] = (
    "disk_warn_percent" in hidden)
checks["…however deeply it is nested"] = (
    "disk_warn_auto_cleanup" in hidden)
checks["…including a textarea, not just inputs"] = (
    "registry_mirrors" in hidden)
checks["a field outside one does not"] = "cron_schedule" not in hidden
checks["…and the block ends where its tag ends"] = "language" not in hidden

# A void element closes nothing — `<hr class="adv-only">` must not
# swallow the rest of the page. That is the bug this parser exists to
# not have.
checks["a void element does not open a scope"] = "language" not in hidden

# Malformed HTML must cost the settings page nothing.
checks["broken markup yields no findings rather than an error"] = (
    settings_notices.hidden_fields("<div class='adv-only'><input name=x") is not None)


# ═══ which of them are actually doing something ══════════════════════
DEFAULTS = {"disk_warn_percent": 85, "disk_warn_auto_cleanup": False,
            "cron_schedule": "0 18 * * *", "registry_mirrors": []}
cfg = types.SimpleNamespace(disk_warn_percent=60, disk_warn_auto_cleanup=True,
                            cron_schedule="0 3 * * *", registry_mirrors=[])
active = settings_notices.active_hidden(cfg, PAGE, DEFAULTS)
names = [k for k, _v, _l in active]
checks["a hidden setting that is on is reported"] = (
    "disk_warn_auto_cleanup" in names)
checks["…and so is one merely moved off its default"] = (
    "disk_warn_percent" in names)
checks["a hidden setting still at its default is not"] = (
    "registry_mirrors" not in names)
checks["a visible setting is never reported, changed or not"] = (
    "cron_schedule" not in names)

checks["values are rendered for a person, not a debugger"] = (
    settings_notices.as_text(True) == "on"
    and settings_notices.as_text(False) == "off"
    and settings_notices.as_text([]) == "—"
    and settings_notices.as_text(["a", "b"]) == "a, b")

# His exact case, end to end: three hosts, one showing it.
his = types.SimpleNamespace(disk_warn_percent=85, disk_warn_auto_cleanup=True,
                            cron_schedule="0 18 * * *", registry_mirrors=[])
checks["his own case produces exactly one line"] = [
    k for k, _v, _l in settings_notices.active_hidden(his, PAGE, DEFAULTS)
] == ["disk_warn_auto_cleanup"]

# And an untouched install produces none — the notice must not become
# furniture that everyone learns to scroll past.
fresh = types.SimpleNamespace(disk_warn_percent=85,
                              disk_warn_auto_cleanup=False,
                              cron_schedule="0 18 * * *", registry_mirrors=[])
checks["an untouched install is told nothing"] = (
    settings_notices.active_hidden(fresh, PAGE, DEFAULTS) == [])

# ═══ the page only discloses in simple mode ══════════════════════════
web_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
i = web_src.index("def _settings_notices")
notices = web_src[i:web_src.index("\n        def ", i + 10)]
checks["the disclosure is scoped to simple mode"] = (
    'ui_mode", "advanced") == "simple"' in notices)
checks["…and offers the switch that reveals them"] = (
    'name="mode" value="advanced"' in notices)
checks["…reading it from the page that was just rendered"] = (
    "page_html" in notices)
# Secrets are named in the override table, never in the hidden-settings
# list — that one prints values.
checks["only loggable keys reach the hidden list"] = (
    "LOGGABLE_PERSISTENT_KEYS" in notices)

# ═══ a way back from an overruled environment variable ═══════════════
from config import Config, PERSISTENT_ENV_DEFAULTS, PERSISTENT_ENV_VARS  # noqa: E402

cfg_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "config.py"), encoding="utf-8").read()
i = cfg_src.index("def refresh_env_overrides")
refresh = cfg_src[i:cfg_src.index("\n    def ", i + 10)]
checks["adopting recomputes the list it came from"] = (
    "_detect_env_overrides" in refresh)

i = cfg_src.index("def adopt_env_value")
adopt = cfg_src[i:cfg_src.index("\n    def ", i + 10)]
checks["adopting refuses a key that is not being overruled"] = (
    "self.env_override(key)" in adopt and "return False" in adopt)
checks["…writes the value in rather than deleting the key"] = (
    "setattr(self, key, seeded[key])" in adopt
    and "self.save_persistent()" in adopt)
checks["…and refreshes, so the row does not survive its own click"] = (
    "self.refresh_env_overrides()" in adopt)

# Deleting the key instead of adopting it would look like it worked and
# come back on the next save, because save_persistent writes every key.
checks["the reasoning is recorded where the next person will look"] = (
    "writes *every* persistent key" in adopt)

# The value must never ride along in the entries: those get iterated,
# serialised and rendered, and half of these keys are secrets. An earlier
# version of this carried it there and test_env_override.py caught it.
checks["raw values stay out of the override entries"] = (
    '"raw"' not in cfg_src.split("overrides.append({")[1].split("})")[0])
checks["…and are read from the private seed map instead"] = (
    "_env_seeded" in adopt)

# End to end against a real Config: env sets a value, a saved file beats
# it, adopting settles it.
with tempfile.TemporaryDirectory() as tmp:
    os.environ["DISK_WARN_AUTO_CLEANUP"] = "true"
    os.environ["DATA_DIR"] = tmp
    with open(os.path.join(tmp, "settings.json"), "w") as f:
        f.write('{"disk_warn_auto_cleanup": false}')
    c = Config.from_env()
    o = c.env_override("disk_warn_auto_cleanup")
    checks["a saved false really does beat an env true"] = (
        o is not None and c.disk_warn_auto_cleanup is False)
    checks["…while the entry itself carries no value for a secret"] = (
        Config._display_value("web_password", "hunter2") is None)
    checks["adopting reports that it did something"] = (
        c.adopt_env_value("disk_warn_auto_cleanup") is True)
    checks["…and refuses a second time, having nothing left to do"] = (
        c.adopt_env_value("disk_warn_auto_cleanup") is False)
    checks["adopting it ends the override"] = (
        c.env_override("disk_warn_auto_cleanup") is None
        and c.disk_warn_auto_cleanup is True)
    # And it stays adopted: the next save must not re-freeze the old one.
    c.save_persistent()
    c2 = Config.from_env()
    checks["…and it survives the next save, which writes every key"] = (
        c2.disk_warn_auto_cleanup is True)
    os.environ.pop("DISK_WARN_AUTO_CLEANUP", None)
    os.environ.pop("DATA_DIR", None)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
