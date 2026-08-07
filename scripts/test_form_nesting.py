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
inside a form — plus the invariant that keeps such a page correct now that
its form is empty and its fields associate by id.

Two pages are built that way since the notification channels moved to a
Connections page of their own, and both are checked here. Generalising it
was worth it twice over: it caught a field the settings handler still read
after its input had left the page, and it caught itself — the first version
of the loop found the *GET* router's `elif path == "/connections":` instead
of the POST handler's, so the symmetry checks were comparing the page
against six lines of routing and passing vacuously.
"""

import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")

#: Fields attach to their page's (empty) form by id. A control without
#: this is rendered, looks right, and is never submitted.
#:
#: Two pages are built this way now: Settings, and the Connections page
#: the notification channels moved to. Each entry is
#: (page name, the empty-form marker that starts its region, the id its
#: controls must carry, the POST path its handler answers).
PAGES = [
    ("Settings",
     '<form method="POST" action="/settings" id="settings-form"></form>',
     'form="settings-form"', "/settings"),
    ("Connections",
     '<form method="POST" action="/connections" id="conn-form"></form>',
     'form="conn-form"', "/connections"),
]


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

    for page, empty_form, form_id, post_path in PAGES:
        method = "def _page" + post_path.replace("/", "_")

        # ── 2. the form is empty and carries the id ──────────────
        checks[f"{page}: form declares its id and is empty"] = empty_form in src
        if empty_form not in src:
            continue

        # ── 3. every control in the template reaches it ──────────
        # The region starts at the page method, not at the form tag.
        # Some controls are assembled into a variable a few lines above
        # the template and interpolated in — the "remove the saved
        # Discord bot token" checkbox is one, because it is only offered
        # when there is a token to remove. Those are as much part of the
        # page as the ones written inline, and a region that stopped at
        # the template start reported the checkbox as a field the handler
        # reads and the page never sends, which was the test being
        # short-sighted rather than the page being wrong.
        i = src.index(empty_form)
        top = src.rindex(method, 0, i)
        region = src[top:src.index('"""', i)]

        # Controls belonging to one of the page's own small POST forms
        # are exempt — those submit to /api/window, /api/maintenance and
        # friends.
        own = [(m.start(), region.find("</form>", m.start()) + 7)
               for m in re.finditer(r"<form\b", region)]
        inside_own = lambda p: any(a <= p < b for a, b in own)

        orphans = []
        for m in re.finditer(
                r'<(input|select|textarea)\b[^>]*\bname="([\w]+)"[^>]*>', region):
            if inside_own(m.start()) or form_id in m.group(0):
                continue
            orphans.append(m.group(2))
        checks[f"{page}: every field is attached to the form"] = not orphans
        if orphans:
            print(f"    {page}: not submitted: {', '.join(sorted(set(orphans)))}")

        # The save button too — a submit button outside the form does
        # nothing at all.
        save = re.search(r'<button type="submit"[^>]*>\{t\("web_save"\)\}', region)
        checks[f"{page}: the save button is attached to the form"] = bool(
            save and form_id in save.group(0))

        # ── 4. what the handler reads is what the page sends ─────
        # The two halves drifting apart is the quieter version of the
        # same bug: a field that is rendered, submitted, and then never
        # looked at. It caught a real one when the channels moved — the
        # settings handler still named a field that had left the page.
        # Search inside do_POST only. The GET router uses the same
        # `elif path == "/connections":` line one method earlier, and
        # slicing from there yields six lines of routing and an empty
        # `wanted` set — which every symmetry check then passes,
        # vacuously.
        lines = src.split("\n")
        post_at = next(i for i, l in enumerate(lines)
                       if l.strip().startswith("def do_POST"))
        start = next(i for i in range(post_at, len(lines))
                     if lines[i].strip() in (f'if path == "{post_path}":',
                                             f'elif path == "{post_path}":'))
        end = next(i for i in range(start + 5, len(lines))
                   if re.match(r"            elif path ==", lines[i]))
        handler = "\n".join(lines[start:end])
        wanted = (set(re.findall(r'["\'](\w+)["\']\s*(?:in|not in)\s+params', handler))
                  | set(re.findall(r'params\[["\'](\w+)["\']\]', handler))
                  | set(re.findall(r'params\.get\(["\'](\w+)["\']', handler)))
        sent = set(re.findall(r'name="([\w]+)"[^>]*' + re.escape(form_id), region))
        sent |= set(re.findall(re.escape(form_id) + r'[^>]*name="([\w]+)"', region))

        checks[f"{page}: the handler reads nothing the page cannot send"] = not (wanted - sent)
        if wanted - sent:
            print(f"    {page}: handler wants but page omits: "
                  f"{', '.join(sorted(wanted - sent))}")
        checks[f"{page}: the page sends nothing the handler ignores"] = not (sent - wanted)
        if sent - wanted:
            print(f"    {page}: page sends but handler ignores: "
                  f"{', '.join(sorted(sent - wanted))}")

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
