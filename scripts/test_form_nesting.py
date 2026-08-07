#!/usr/bin/env python3
"""No nested <form> in the Web UI, and every settings field reaches the form.

This exists because of a bug that was invisible three ways over. Moving the
maintenance and update-window cards into their tab panes put their own
little POST forms inside the big settings form. Nested forms are invalid
HTML, and the parser does not merely tolerate them — it *drops* the inner
start tag and lets the inner `</form>` close the OUTER form.

Measured consequence at the time: the settings form ended after the Updates
tab, and 23 fields from Cleanup, Notifications and Channels fell outside it.
Saving would have submitted none of them, and a checkbox that is not
submitted reads as "off" — so one click would have silently turned off
auto-cleanup, monitoring and the weekly report and blanked both webhooks.

Why a test rather than care: the page *looked* perfect. `ast.parse` passed,
the HTML structure check passed, and a screenshot of every tab showed every
card in its right place. Only reading `form.elements` out of a real browser
showed it. So this asserts the one property that fails first — no form
inside a form — plus the invariant that keeps the settings page correct now
that its form is empty and its fields associate by id.
"""

import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")

#: Fields inside the tab panes attach to the (empty) settings form by id.
#: A control without this is rendered, looks right, and is never submitted.
FORM_ID = 'form="settings-form"'


def form_depth_violations(text):
    """Every position where a <form> opens while another is still open."""
    bad, depth = [], 0
    for m in re.finditer(r"<form\b|</form>", text):
        if m.group(0) == "</form>":
            depth = max(0, depth - 1)
        else:
            if depth:
                bad.append(m.start())
            depth += 1
    return bad


def main():
    src = open(SRC).read()
    checks = {}

    # ── 1. no nested form anywhere in the file ────────────────────
    # Checked over the whole source, not just the settings page: the same
    # mistake is available on every page that renders a card with a button.
    bad = form_depth_violations(src)
    checks["no <form> opens inside another"] = not bad
    if bad:
        for pos in bad[:5]:
            line = src.count("\n", 0, pos) + 1
            print(f"    nested form at web_ui.py:{line}")

    # ── 2. the settings form is empty and carries the id ─────────
    checks["settings form declares its id"] = (
        '<form method="POST" action="/settings" id="settings-form"></form>' in src)

    # ── 3. every control in the settings template reaches it ─────
    # Bounded by the empty form tag and the closing of the template's
    # string, which is where the settings markup lives. If the form is not
    # empty the earlier checks have already said so; bail rather than
    # raising, so the report stays readable.
    # The region starts at `def _page_settings`, not at the form tag.
    # Some controls are assembled into a variable a few lines above the
    # template and interpolated in — the "remove the saved Discord bot
    # token" checkbox is one, because it is only offered when there is a
    # token to remove. Those are as much part of the page as the ones
    # written inline, and a region that stopped at the template start
    # reported the checkbox as a field the handler reads and the page
    # never sends, which was the test being short-sighted rather than
    # the page being wrong.
    marker = 'id="settings-form"></form>'
    if marker not in src:
        checks["the settings form is empty"] = False
        region = ""
    else:
        i = src.index(marker)
        top = src.rindex("def _page_settings", 0, i)
        region = src[top:src.index('"""', i)]

    # Controls belonging to one of the page's own small POST forms are
    # exempt — those submit to /api/window, /api/maintenance and friends.
    own = [(m.start(), region.find("</form>", m.start()) + 7)
           for m in re.finditer(r"<form\b", region)]
    inside_own = lambda p: any(a <= p < b for a, b in own)

    orphans = []
    for m in re.finditer(r'<(input|select|textarea)\b[^>]*\bname="([\w]+)"[^>]*>',
                         region):
        if inside_own(m.start()) or FORM_ID in m.group(0):
            continue
        orphans.append(m.group(2))
    checks["every settings field is attached to the form"] = not orphans
    if orphans:
        print(f"    not submitted: {', '.join(sorted(set(orphans)))}")

    # The save button too — a submit button outside the form does nothing.
    save = re.search(r'<button type="submit"[^>]*>\{t\("web_save"\)\}', region)
    checks["the save button is attached to the form"] = bool(
        save and FORM_ID in save.group(0))

    # ── 4. what the handler reads is what the page sends ─────────
    # The two halves drifting apart is the quieter version of the same bug:
    # a field that is rendered, submitted, and then never looked at.
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == 'if path == "/settings":')
    end = next(i for i in range(start + 5, len(lines))
               if re.match(r"            elif path ==", lines[i]))
    handler = "\n".join(lines[start:end])
    wanted = (set(re.findall(r'["\'](\w+)["\']\s*(?:in|not in)\s+params', handler))
              | set(re.findall(r'params\[["\'](\w+)["\']\]', handler))
              | set(re.findall(r'params\.get\(["\'](\w+)["\']', handler)))
    sent = set(re.findall(r'name="([\w]+)"[^>]*' + re.escape(FORM_ID), region))
    sent |= set(re.findall(re.escape(FORM_ID) + r'[^>]*name="([\w]+)"', region))

    checks["the handler reads nothing the page cannot send"] = not (wanted - sent)
    if wanted - sent:
        print(f"    handler wants but page omits: {', '.join(sorted(wanted - sent))}")
    checks["the page sends nothing the handler ignores"] = not (sent - wanted)
    if sent - wanted:
        print(f"    page sends but handler ignores: {', '.join(sorted(sent - wanted))}")

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
