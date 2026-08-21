#!/usr/bin/env python3
"""Both front ends say the same thing about the changelog (#63).

@NotRetarded put Telegram's `/changelog` next to Discord's and they
disagreed. The extraction had shared the *reading* — one module fetches
the file and compares versions — but each front end still decided for
itself what to say and how, so they drifted:

  * Telegram, when you are on the newest version, also shows what THAT
    version brought (#2, @famewolf, so "what did I just get?" is
    answerable after a self-update). Discord said one sentence and
    stopped.
  * Telegram adapted GitHub's markdown; Discord printed it raw, headings
    and all.
  * Telegram named the truncation and linked the full file; Discord cut
    mid-sentence.

So the decision moved into the core too: `report()` answers WHICH of the
three things to say, `render_body()` lays an entry out with the caller's
bold marker. Only the marker and the length cap differ now — the same
arrangement `status_render` uses for `/status`.

Discord stays English while Telegram is translated; that is true of every
Discord command, not this one, and is not what drifted here.
"""
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import changelog  # noqa: E402

SAMPLE = """# Changelog

## [2.0.0] - 2026-08-20
### Fixed
- **A thing** broke ![shot](http://x/y.png)

## [1.9.0] - 2026-08-18
### Added
- Something
"""

checks = {}

# ── one decision, three cases ────────────────────────────────────────
r = changelog.report(SAMPLE, "1.9.0")
checks["a newer release is reported as newer"] = (
    r["kind"] == "newer" and [v for v, _, _ in r["entries"]] == ["2.0.0"])
r = changelog.report(SAMPLE, "2.0.0")
checks["the newest version reports what IT brought"] = (
    r["kind"] == "current" and r["current"][0] == "2.0.0"
    and "A thing" in r["current"][2])
r = changelog.report(SAMPLE, "9.9.9")
checks["a version not in the file says so, without inventing one"] = (
    r["kind"] == "unknown" and r["current"] is None)

# ── the body: same content, different marker ─────────────────────────
body = changelog.report(SAMPLE, "1.9.0")["entries"][0][2]
tg = changelog.render_body(body, bold="*")
dc = changelog.render_body(body, bold="**")
strip = lambda s: s.replace("**", "").replace("*", "")  # noqa: E731
checks["both front ends render the same content"] = strip(tg) == strip(dc)
checks["…Telegram gets single asterisks it can parse"] = (
    "*Fixed*" in tg and "**" not in tg)
checks["…Discord gets its own double asterisks"] = "**Fixed**" in dc
checks["a GitHub heading becomes bold, not a stray ###"] = (
    "###" not in tg and "###" not in dc)
checks["an image embed is dropped rather than shown raw"] = (
    "![" not in tg and "![" not in dc)

# ── both front ends actually go through the core ─────────────────────
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Telegram asks the core what to say"] = "changelog.report(" in tsrc
checks["…and Discord asks the same question"] = "changelog.report(" in dsrc
checks["Telegram renders through the shared body renderer"] = (
    "changelog.render_body(" in tsrc)
checks["…and so does Discord"] = "changelog.render_body(" in dsrc

# The three cases must all exist on the Discord side — the missing
# "current" case is exactly what he photographed.
i = dsrc.index("def _cmd_changelog")
dbody = dsrc[i:dsrc.index("\n    def ", i + 10)]
for kind in ("unknown", "current"):
    checks[f"Discord handles the '{kind}' case"] = f'"{kind}"' in dbody
checks["Discord names its truncation and links the file"] = (
    "truncated" in dbody and "CHANGELOG.md" in dbody)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
