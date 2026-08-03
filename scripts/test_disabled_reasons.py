#!/usr/bin/env python3
"""A disabled button must say why it is disabled (#55, @LeeNX).

`MONITOR_ONLY` greys out every action that would change a container. The
update and auto-update buttons explained themselves; pin, restart, stop and
the major-confirm toggle just went dead with their normal tooltip still on
them — "pin this container" on a button that cannot be clicked.

@LeeNX found it on pin. The same omission was on three more, which is the
part worth a test: the fix is one line per button and there is nothing to
stop the next button from being added without it. So this asserts the rule
rather than the four instances — every control the monitor-only flag
disables must reference the monitor-only tooltip.
"""

import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")

#: The flag that greys the button out, and the tooltip that explains it.
DISABLE = ("_mo_off", "_monitor_only")
EXPLAIN = "_mo_title"


def buttons(region):
    """Every <button …> in the row markup, as (line_offset, tag_text)."""
    out = []
    for m in re.finditer(r"<button\b", region):
        end = region.find("</button>", m.start())
        if end < 0:
            continue
        out.append((region.count("\n", 0, m.start()), region[m.start():end]))
    return out


def main():
    src = open(SRC, encoding="utf-8").read()
    checks = {}

    start = src.index("_monitor_only = checker.is_monitor_only")
    end = src.index('actions = f\'<div class="btn-row">', start)
    region = src[start:end]
    base = src.count("\n", 0, start) + 1

    offenders = []
    disabled_count = 0
    for off, tag in buttons(region):
        # Only buttons the flag actually disables. A button that stays live
        # under monitor-only (the check button, deliberately) is not in
        # scope — knowing an update exists is the point of still watching.
        if not any(d in tag for d in DISABLE):
            continue
        disabled_count += 1
        title = re.search(r'title="([^"]*)"', tag)
        if not title or EXPLAIN not in title.group(1):
            offenders.append((base + off, tag[:70]))

    checks["the row has buttons disabled by monitor-only"] = disabled_count >= 4
    checks["every one of them names the reason"] = not offenders
    for line, tag in offenders:
        print(f"    web_ui.py:{line}  {tag}")

    # The pin button is computed separately (its title has three branches),
    # so assert its precedence explicitly: monitor-only outranks the
    # label-authoritative note, because it is the reason for the disable.
    pin = re.search(r"_pin_title = \((.*?)\n\s*$", src, re.S | re.M)
    pin_src = src[src.index("_pin_title = ("):src.index("pin_btn = (")]
    checks["pin explains monitor-only first"] = (
        pin_src.index(EXPLAIN) < pin_src.index("web_label_authoritative"))

    # And the marker that sits next to a name is not flush against it.
    css = open(os.path.join(os.path.dirname(__file__), "..", "app", "static",
                            "app.css"), encoding="utf-8").read()
    mark = re.search(r"\.label-mark\s*\{([^}]*)\}", css)
    checks["the label marker keeps its distance"] = bool(
        mark and re.search(r"margin-left:\s*[.\d]", mark.group(1)))

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
