#!/usr/bin/env python3
"""The self-update trigger is reachable from the icon bar, not only Settings.

@LeeNX, #2: "Can we add the update button for dockSentry in the icon bar.
I keep having to go looking for it in the settings and always battle to
find it, as it's under `Cleanup`, which seems so odd, plus don't know why
it can't have the same force update now button like the others and just
point to the self-updater."

So this asserts the INTENT, not the markup: from an ordinary page, without
opening Settings, there is a control that posts to the endpoint the
self-updater actually listens on, it still asks before it fires, and it is
absent when Docksentry cannot identify its own container — because on such
a host (QNAP, some Podman setups) the swap it offers cannot be performed
and the button would be a promise we cannot keep.

The header comes from `_render_page`, which both front ends go through, so
the same checks are run with the classic table and with the V2 list
selected. Nothing here touches Docker or a socket: the handler class is
built via create_handler() and instantiated with __new__, the way
scripts/test_web_selfupdate_row.py does it.
"""
import inspect
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

MISSING = "/nonexistent/docksentry-test"

#: Where the self-update lives. Both the old place and the new one must
#: point here — one trigger, two doors, nothing to keep in step.
ENDPOINT = "/api/selfupdate"


def _config(status_view="table"):
    return types.SimpleNamespace(
        language="en",
        ui_mode="advanced",
        debug=False,
        status_view=status_view,
        auto_selfupdate=False,
        maintenance_file=MISSING,
        pending_file=MISSING,
        history_file=MISSING,
    )


class FakeChecker:
    def __init__(self, own_name):
        self._own = own_name

    def _own_container_name(self):
        return self._own


def render_frame(own_name="docksentry", status_view="table", page="status"):
    """The page chrome around some content, as a browser would get it."""
    cfg = _config(status_view)
    handler_cls = web_ui.create_handler(
        cfg, FakeChecker(own_name), bot=None, store=None)
    h = handler_cls.__new__(handler_cls)
    h.path = "/"
    h._session_user = lambda: "admin"
    return h._render_page("<p>content</p>", page), handler_cls


def header_of(html):
    """Just the icon bar — a hit anywhere else on the page proves nothing."""
    start = html.find('<div class="header">')
    end = html.find('<div class="nav-wrap', start)
    return html[start:end] if start >= 0 and end > start else ""


def main():
    checks = {}

    html, handler_cls = render_frame()
    head = header_of(html)

    checks["the icon bar is found at all (guards the rest)"] = bool(head)

    # ── The ask ───────────────────────────────────────────────────────
    checks["icon bar offers a self-update"] = ENDPOINT in head
    checks["…as a POST, not a link to the settings page"] = (
        'method="POST"' in head and f'action="{ENDPOINT}"' in head)
    checks["…and it is a button you can press"] = 'type="submit"' in head

    # It has to follow you around, or the hunt is only shorter, not over.
    checks["…and it is there on every page, not just one"] = all(
        ENDPOINT in header_of(render_frame(page=p)[0])
        for p in ("status", "history", "logs", "settings"))

    # ── It points at the real self-updater ───────────────────────────
    # Not a new path invented for the button: the handler must already
    # dispatch it.
    routed = ENDPOINT in inspect.getsource(handler_cls.do_POST)
    checks["the endpoint is one the server actually handles"] = routed

    # …and the old door is still there, so nobody's bookmark breaks.
    checks["Settings still offers it too"] = (
        ENDPOINT in inspect.getsource(handler_cls._page_settings))

    # ── It still asks first ──────────────────────────────────────────
    # A self-update takes the Web UI offline for a moment. The one in
    # Settings confirms; a header button one stray click away must too.
    checks["it confirms before firing"] = "data-confirm=" in head
    checks["…with its own title and label, like the Settings one"] = (
        "data-confirm-title=" in head and "data-confirm-label=" in head)
    checks["…and is marked destructive"] = "data-confirm-danger=" in head

    # ── Wording comes from the language files ────────────────────────
    import i18n  # noqa: E402 — only needed for this check
    tip = i18n.get_translator("en")("web_selfupdate_toolbar_tt")
    checks["the tooltip is a real string, not a key"] = (
        tip != "web_selfupdate_toolbar_tt")
    checks["…and it is what the button carries"] = tip[:40] in head

    # ── Both front ends, because the frame is shared ─────────────────
    v2_head = header_of(render_frame(status_view="list")[0])
    checks["the V2 list gets the same icon bar button"] = ENDPOINT in v2_head

    # ── Absent when a self-update cannot work ────────────────────────
    # Same condition the status table uses to decide `is_self`: no own
    # container name, no self-anything.
    blind_head = header_of(render_frame(own_name="")[0])
    checks["no own container name -> no button"] = ENDPOINT not in blind_head
    checks["…but the rest of the icon bar survives"] = (
        "/api/ui_mode" in blind_head)
    blind_v2 = header_of(render_frame(own_name="", status_view="list")[0])
    checks["…in V2 as well"] = ENDPOINT not in blind_v2

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
