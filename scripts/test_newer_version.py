#!/usr/bin/env python3
"""A pinned tag never reports an update — and the tag matcher that says so.

Two things, one of which was a live defect.

**The advisory (#33, @LeeNX).** A pinned version tag is immutable, so its
digest never moves and the check reports "up to date" forever — including
long after a newer version shipped. That reading is true and misleading at
once, and he asked for it to be explained; the issue was closed answering a
different question. Docksentry now says that a newer version exists, and
still does not touch the container: pinning `1.25.3` is a statement of
intent, and moving to `1.26` unasked would override it. Advisory entries
live in their own file precisely so nothing that reads pending updates can
mistake one for something to apply.

**The tag matcher was matching the wrong tags.** `get_highest_semver_tag`
filtered candidates with `if prefix and not ts.startswith(prefix)` — so a
current tag beginning with a digit, which is the common case, produced an
empty prefix and skipped the check entirely. Everything the SemVer pattern
would swallow then qualified, and the pattern allows a leading `something-`.

Measured against the real registry before the fix:

    linuxserver/qbittorrent:4.6.5  ->  arm64v8-20.04.1

An Ubuntu version, on the wrong architecture. This function is not new and
is not only used for advisories: `_is_major_bump` calls it, so anyone
running linuxserver images with major-confirmation enabled was asked to
confirm every ordinary patch update as a major bump.

The second half of that is a repository carrying two numbering schemes in
the same shape — qbittorrent's own 4.6.5 beside its Ubuntu base 20.04.1 —
which no amount of prefix matching separates. That is handled by a stated
heuristic rather than pretended away.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker


def checker(tags):
    o = UpdateChecker.__new__(UpdateChecker)
    o.debug = False
    o._debug = lambda m: None
    o._list_remote_tags = lambda reg, repo: tags
    return o


def main():
    checks = {}

    # ── the defect that shipped ──────────────────────────────────
    # A digit-initial tag must not match an arch- or distro-prefixed one.
    real = ["4.6.5", "4.6.6", "arm64v8-4.6.6", "arm64v8-20.04.1",
            "amd64-4.6.6", "20.04.1", "latest"]
    best, parsed = checker(real).get_highest_semver_tag("r", "p", "4.6.5")
    checks["an arch-prefixed tag is not a candidate"] = (
        best is not None and not best.startswith(("arm64", "amd64")))
    checks["the real next version wins"] = best == "4.6.6"

    # A prefixed current tag matches only the same prefix.
    pre = ["v1.2.3", "v1.3.0", "1.9.0", "beta-2.0.0"]
    best, _ = checker(pre).get_highest_semver_tag("r", "p", "v1.2.3")
    checks["a v-prefix matches only v-prefixes"] = best == "v1.3.0"
    # The function returns the highest CANDIDATE, equal-to-current
    # included — the caller decides whether that is newer. What matters
    # here is that a v-prefixed tag is not among the candidates at all.
    best, _ = checker(pre).get_highest_semver_tag("r", "p", "1.9.0")
    checks["…and a bare tag never matches a v-prefixed one"] = (
        best is None or not best.startswith("v"))

    # ── two numbering schemes in one repository ──────────────────
    # qbittorrent's own version beside its Ubuntu base. Nothing in the tag
    # text separates them, so a stated heuristic does.
    mixed = ["4.6.5", "5.2.3", "20.04.1", "22.04.2"]
    best, parsed = checker(mixed).get_highest_semver_tag("r", "p", "4.6.5")
    checks["an implausible leap is skipped"] = best == "5.2.3"
    # And the guard must not eat a real major jump. radarr 5 -> 6 happened.
    real_major = ["5.12.2", "6.3.0"]
    best, _ = checker(real_major).get_highest_semver_tag("r", "p", "5.12.2")
    checks["a genuine major jump survives"] = best == "6.3.0"

    # ── the advisory itself ──────────────────────────────────────
    o = checker(["1.30.0", "1.37.1"])
    checks["a newer version is reported"] = (
        o._newer_version_available("r", "p", "1.30.0") == "1.37.1")
    o = checker(["1.30.0"])
    checks["nothing to report stays silent"] = (
        o._newer_version_available("r", "p", "1.30.0") == "")
    # A moving tag has no "newer": the digest check already answers it.
    o = checker(["latest", "1.0.0", "2.0.0"])
    checks["a moving tag is not advised on"] = (
        o._newer_version_available("r", "p", "latest") == "")
    # Two-component tags do not parse as SemVer — a known limitation, and
    # asserted so it is a decision rather than a surprise.
    o = checker(["16.3", "16.4"])
    checks["a two-component tag is a known gap"] = (
        o._newer_version_available("r", "p", "16.3") == "")
    # A lookup that throws must never break a check that had succeeded.
    o = UpdateChecker.__new__(UpdateChecker)
    o.debug = False
    o._debug = lambda m: None
    o._list_remote_tags = lambda reg, repo: (_ for _ in ()).throw(RuntimeError("x"))
    checks["a failed lookup is swallowed"] = (
        o._newer_version_available("r", "p", "1.0.0") == "")

    # ── it must never become a pending update ────────────────────
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "update_checker.py"), encoding="utf-8").read()
    seg = src[src.index("_newer_version_available(registry"):]
    seg = seg[:seg.index("except Exception")]
    checks["the advisory is not appended to updates"] = "updates.append" not in seg
    checks["it goes to its own store"] = "advisories[" in seg

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
