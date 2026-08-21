#!/usr/bin/env python3
"""`/changelog` compares versions correctly, betas included (#63).

On a beta, `/changelog` reported nonsense: the owner ran it on
`2.18.0-beta.12` and was told "206 new versions since v2.18.0-beta.12",
with v2.17.0 — an OLDER release — listed among them. Two faults, both in
the version handling:

1. The heading pattern matched only `## [x.y.z]`, so every `-beta.N`
   heading was invisible to the parser.
2. `int(x) for x in "2.18.0-beta.12".split(".")[:3]` threw on "0-beta",
   was caught, and fell back to (0,0,0) — against which every one of the
   206 historical stable entries looked newer.

The fix is one comparable key that understands prereleases: a final
release ranks above its own betas, and both above the previous version.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import changelog

key = changelog.version_key

SAMPLE = """# Changelog

## [2.18.0-beta.12] - 2026-08-20
twelfth beta

## [2.18.0-beta.2] - 2026-08-18
second beta

## [2.17.0] - 2026-08-18
prior stable

## [2.16.1] - 2026-08-17
older
"""

checks = {}

# ── the key orders prereleases the way semver does ───────────────────
checks["a beta is newer than the previous stable"] = (
    key("2.18.0-beta.12") > key("2.17.0"))
checks["the final release is newer than its own beta"] = (
    key("2.18.0") > key("2.18.0-beta.12"))
checks["a later beta is newer than an earlier one"] = (
    key("2.18.0-beta.12") > key("2.18.0-beta.2"))
checks["a plain stitched version still parses"] = (
    key("2.17.0") > key("2.16.1"))
checks["garbage sorts to the bottom, never crashes"] = (
    key("not-a-version") == (0, 0, 0, 0, 0))

# ── the reported bug: on the newest beta, nothing is newer ───────────
newer = changelog.parse_entries(SAMPLE, "2.18.0-beta.12")
checks["on the newest beta, no entry is reported as newer"] = (
    newer == [])
checks["…so v2.17.0 is NOT called a new version"] = (
    "2.17.0" not in [v for v, _, _ in newer])

# ── from an older stable, exactly the newer ones, newest first ───────
newer2 = changelog.parse_entries(SAMPLE, "2.17.0")
checks["from 2.17.0, the betas ahead of it are returned"] = (
    [v for v, _, _ in newer2] == ["2.18.0-beta.12", "2.18.0-beta.2"])
checks["…and 2.17.0 itself is not among them"] = (
    "2.17.0" not in [v for v, _, _ in newer2])

# ── beta headings are parseable at all (regex fix) ───────────────────
checks["a beta heading is found by the exact-entry lookup"] = (
    (changelog.entry_for(SAMPLE, "2.18.0-beta.12") or (None,))[0]
    == "2.18.0-beta.12")
checks["…and its body comes back with it"] = (
    "twelfth beta" in (changelog.entry_for(
        SAMPLE, "2.18.0-beta.12") or (None, None, ""))[2])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
