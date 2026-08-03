#!/usr/bin/env python3
"""Every icon key the templates use must exist.

Written because it did not. Swapping the SVG set for emoji dropped
`arrow_up`, which is referenced once — on the settings page — and that
page raised KeyError and served a 500 from then on. Nothing else noticed:
the status page rendered fine, the tests passed, and the break was only
found by opening the page.

A missing key cannot be caught by reading the dict, because the usages are
spread across template strings in three modules. So it is checked, not
reviewed.
"""

import glob
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)


def main():
    checks = {}

    src = open(os.path.join(APP, "web_ui.py")).read()
    i = src.index("_ICONS = {")
    j = src.index("\n}", i)
    defined = set(re.findall(r'"([a-z_]+)":', src[i:j]))

    used = set()
    for f in glob.glob(os.path.join(APP, "*.py")):
        used |= set(re.findall(r'_ICONS\[["\']([a-z_]+)["\']\]', open(f).read()))

    missing = used - defined
    checks["every used icon key is defined"] = not missing
    if missing:
        print(f"    missing: {sorted(missing)}")
    checks["there are icons at all"] = len(defined) >= 8
    # Not an error, but worth seeing: a key nobody uses is dead weight and
    # usually the leftover of a rename.
    unused = defined - used
    if unused:
        print(f"    (defined but unused, harmless: {sorted(unused)})")

    # Every glyph is wrapped so the CSS can size it. A bare emoji inherits
    # the button's text metrics and sits a few pixels off from its
    # neighbours.
    body = src[i:j]
    entries = re.findall(r'"([a-z_]+)":\s*\'([^\']*)\'', body)
    checks["every glyph is wrapped for sizing"] = all(
        'class="ic"' in v for _, v in entries)
    checks["all entries parsed"] = len(entries) == len(defined)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
