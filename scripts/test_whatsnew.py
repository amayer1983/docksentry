#!/usr/bin/env python3
"""Tell people what the version they just pulled actually changed.

The self-update path has announced itself since v1.17.0, but that is the
minority route. Most people update Docksentry the way they update
everything else — `docker pull` and `up -d` — and that was completely
silent. Features shipped and then sat unused because nobody was told they
existed.

The headlines are parsed out of CHANGELOG.md, which now ships in the image
for this. Parsed at runtime rather than baked into a summary at build time,
so the message and the file people read on GitHub cannot drift apart.

The case worth testing hardest is the one that is easy to get wrong and
embarrassing to ship: a FIRST-EVER boot has no previous version to compare
against, and announcing "updated to v1.75.0" on a fresh install is simply
untrue. Silence there, and the version recorded for next time.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import whatsnew

SAMPLE = """# Changelog

## [2.0.0] - 2026-08-04

Some prose introducing the release.

### Added
- **A big new thing.** With an explanation that runs on for a while and
  should not end up in the message.
- **Another thing**, described here.

### Fixed
- **A bug that mattered.** More prose.
- a line with no bold lead, which is not a headline
- **A big new thing.** deliberately repeated

## [1.9.0] - 2026-08-01

### Fixed
- **Something older.** Prose.

## [1.8.0] - 2026-07-30

No bold entries at all in this one, just prose about what changed.
"""


def main():
    checks = {}

    # ── headlines come out in order, deduplicated, capped ────────
    h = whatsnew.headlines("2.0.0", SAMPLE)
    checks["headlines are found"] = h[:3] == [
        "A big new thing", "Another thing", "A bug that mattered"]
    checks["only the bold lead is taken"] = not any(
        "explanation" in x for x in h)
    checks["a repeat is dropped"] = h.count("A big new thing") == 1
    checks["a plain line is not a headline"] = not any(
        "no bold lead" in x for x in h)
    checks["the trailing full stop goes"] = not any(
        x.endswith(".") for x in h)

    # It must stop at the next version, or every release announces the
    # whole history.
    checks["it stops at the next version"] = "Something older" not in h
    checks["an older section reads on its own"] = (
        whatsnew.headlines("1.9.0", SAMPLE) == ["Something older"])

    # ── nothing to quote ─────────────────────────────────────────
    # Better to send the link alone than to quote an arbitrary first line
    # and present it as a summary.
    checks["a section with no headlines yields none"] = (
        whatsnew.headlines("1.8.0", SAMPLE) == [])
    checks["an unknown version yields none"] = (
        whatsnew.headlines("9.9.9", SAMPLE) == [])
    checks["a missing changelog yields none"] = (
        whatsnew.headlines("2.0.0", "") == [])

    # ── the cap ──────────────────────────────────────────────────
    many = "## [3.0.0]\n" + "".join(
        f"- **Headline {i}.** prose\n" for i in range(20))
    checks["the headline list is capped"] = (
        len(whatsnew.headlines("3.0.0", many)) == whatsnew.MAX_HEADLINES)

    # ── the message ──────────────────────────────────────────────
    msg = whatsnew.summary("1.9.0", "2.0.0")
    checks["the message names both versions"] = (
        "1.9.0" in msg and "2.0.0" in msg)
    checks["the message links the release"] = (
        whatsnew.release_url("2.0.0") in msg)
    checks["the link points at a tag, not the repo root"] = (
        whatsnew.release_url("2.0.0").endswith("/releases/tag/v2.0.0"))

    # ── the real changelog parses ────────────────────────────────
    # The sample above proves the parser; this proves the FORMAT this
    # project actually writes in still matches it. A changelog style change
    # would otherwise quietly turn every announcement into a bare link.
    from version import VERSION
    real = whatsnew.headlines(VERSION)
    checks[f"the shipped changelog yields headlines for v{VERSION}"] = bool(real)
    if real:
        print(f"    v{VERSION}: {'; '.join(real)[:110]}")

    # ── the guard against announcing on a fresh install ──────────
    # main.py owns the decision; assert the two conditions it must apply,
    # because getting this wrong means every new user is told they just
    # upgraded from nothing.
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                                 "main.py"), encoding="utf-8").read()
    seg = main_src[main_src.index("whatsnew_msg = \"\""):]
    seg = seg[:seg.index("if not post_selfupdate_restart")]
    checks["a previous version is required"] = "if _prev and _prev !=" in seg
    checks["the version is recorded for next time"] = "atomic_write_json" in seg

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
