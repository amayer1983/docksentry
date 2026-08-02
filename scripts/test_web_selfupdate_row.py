#!/usr/bin/env python3
"""Web UI render test for the Docksentry row in the status table (#51).

AUTO_SELFUPDATE was invisible in the UI: the Auto column read the
`docksentry.auto` label / the per-container opt-in list, neither of which
governs our own updates, so our row fell to "—" even with
AUTO_SELFUPDATE=true. And the Auto toggle rendered fully active on that row
while a click did nothing that survived the next boot.

This is the first render test for the Web UI at all. It builds the handler
class via create_handler(), instantiates it without a socket (__new__) and
replaces _send_html / _get_containers, so _page_status() can be called
against fake Docker data. No Docker, no network, no sockets.

Cases:
  - auto_selfupdate=True  -> our row shows the auto badge + the ⚙ marker
  - auto_selfupdate=False -> our row shows "—" (plus the marker)
  - docksentry.auto label on our own container does NOT override
    AUTO_SELFUPDATE
  - empty own-name (QNAP/Podman) -> exactly the old behaviour
  - foreign containers unaffected
  - our row no longer posts to /api/autoupdate; it links to Settings

Exits non-zero on any failure.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

MISSING = "/nonexistent/docksentry-test"


class FakeStore:
    """Minimal ContainerStore stand-in — only what _page_status touches."""

    def __init__(self, auto=(), pinned=()):
        self._auto = list(auto)
        self._pinned = list(pinned)

    def get_pinned(self):
        return list(self._pinned)

    def get_autoupdate(self):
        return list(self._auto)

    def get_ask_before_major(self):
        return []

    def get_groups(self):
        return {}

    def get_notes(self):
        return {}

    def get_links(self):
        # Read once per render for the 🔗 column (#52).
        return {}

    def get_pending_major(self):
        return {}

    def is_protect_stop(self, name):
        return False


class FakeChecker:
    def __init__(self, own_name):
        self._own = own_name

    def _own_container_name(self):
        return self._own

    def get_disk_usage(self):
        return 42.0, 100 * 1024 ** 3, 200 * 1024 ** 3


def _config(auto_selfupdate):
    return types.SimpleNamespace(
        language="en",
        auto_selfupdate=auto_selfupdate,
        ui_mode="advanced",
        debug=False,
        disk_warn_percent=85,
        pending_file=MISSING,
        history_file=MISSING,
        maintenance_file=MISSING,
    )


def render(own_name="docksentry", auto_selfupdate=False, containers=None,
           auto_list=()):
    """Render /status and return the HTML."""
    cfg = _config(auto_selfupdate)
    checker = FakeChecker(own_name)
    store = FakeStore(auto=auto_list)
    handler_cls = web_ui.create_handler(cfg, checker, bot=None, store=store)

    # No socket: BaseHTTPRequestHandler.__init__ would try to talk to one.
    h = handler_cls.__new__(handler_cls)
    h.path = "/"
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._get_containers = lambda: list(containers or [])
    h._page_status()
    return out.get("html", "")


def row_for(html, name):
    """Return the <tr> block of the row linking to /container/<name>.

    Scoped to the TABLE. The status page renders every container twice —
    once as a row, once as a card for narrow screens, both from the same
    locals so their state cannot disagree. Splitting the whole page on
    `<tr>` used to be unambiguous and no longer is: the card carries the
    same `/container/<name>` link, so an unscoped search can return card
    markup and assert against the wrong layout.
    """
    start = html.find('<table id="ctbl"')
    end = html.find("</table>", start)
    table = html[start:end] if start >= 0 else html
    marker = f'/container/{name}"'
    for chunk in table.split("<tr>"):
        if marker in chunk:
            return chunk
    return ""


def c(name, labels=None):
    return {"name": name, "image": f"{name}:latest", "health": "healthy",
            "labels": labels or {}, "version": "", "short_id": "abc123456789"}


def main():
    checks = {}
    both = [c("docksentry"), c("nginx")]

    # ── AUTO_SELFUPDATE=true — our row must say so ────────────────────
    html = render(auto_selfupdate=True, containers=both)
    own = row_for(html, "docksentry")
    checks["auto_selfupdate=True: own row shows the auto badge"] = (
        "badge-purple" in own and ">auto</span>" in own)
    checks["auto_selfupdate=True: own row carries the ⚙ marker"] = (
        'class="self-mark"' in own and "⚙" in own)
    checks["auto_selfupdate=True: own row does not use the 🏷 label marker"] = (
        'class="label-mark"' not in own)

    # ── AUTO_SELFUPDATE=false — "—", but still marked ─────────────────
    html_off = render(auto_selfupdate=False, containers=both)
    own_off = row_for(html_off, "docksentry")
    checks["auto_selfupdate=False: own row shows —"] = (
        '<span class="muted">—</span>' in own_off)
    checks["auto_selfupdate=False: own row still carries the marker"] = (
        'class="self-mark"' in own_off)

    # ── The toggle is gone: no POST to /api/autoupdate on our row ─────
    checks["own row does not post to /api/autoupdate"] = (
        "/api/autoupdate" not in own and "/api/autoupdate" not in own_off)
    checks["own row links to Settings › Updates instead"] = (
        "/settings#updates" in own and "/settings#updates" in own_off)

    # ── A docksentry.auto label on ourselves must NOT win ─────────────
    labelled = [c("docksentry", {"docksentry.auto": "true"}), c("nginx")]
    html_lab = render(auto_selfupdate=False, containers=labelled)
    own_lab = row_for(html_lab, "docksentry")
    checks["docksentry.auto=true on ourselves does not override AUTO_SELFUPDATE=false"] = (
        '<span class="muted">—</span>' in own_lab and ">auto</span>" not in own_lab)
    html_lab2 = render(auto_selfupdate=True,
                       containers=[c("docksentry", {"docksentry.auto": "false"}),
                                   c("nginx")])
    checks["docksentry.auto=false on ourselves does not override AUTO_SELFUPDATE=true"] = (
        ">auto</span>" in row_for(html_lab2, "docksentry"))

    # ── Foreign containers untouched ──────────────────────────────────
    foreign = row_for(html, "nginx")
    checks["foreign row keeps the /api/autoupdate toggle"] = (
        'action="/api/autoupdate"' in foreign)
    checks["foreign row has no self marker"] = 'class="self-mark"' not in foreign
    checks["foreign row not auto without opt-in"] = (
        '<span class="muted">—</span>' in foreign)
    html_optin = render(auto_selfupdate=False, containers=both,
                        auto_list=["nginx"])
    checks["foreign row auto via the opt-in list"] = (
        ">auto</span>" in row_for(html_optin, "nginx"))
    html_flab = render(auto_selfupdate=False,
                       containers=[c("docksentry"),
                                   c("nginx", {"docksentry.auto": "true"})])
    f_lab = row_for(html_flab, "nginx")
    checks["foreign row still honours the docksentry.auto label"] = (
        ">auto</span>" in f_lab and 'class="label-mark"' in f_lab)

    # ── Empty own-name (QNAP / Podman) = unchanged old behaviour ──────
    # AUTO_SELFUPDATE must not leak into any row, and the toggle stays.
    html_noself = render(own_name="", auto_selfupdate=True, containers=both)
    checks["empty own_name: no row treated as self"] = (
        'class="self-mark"' not in html_noself)
    for n in ("docksentry", "nginx"):
        r = row_for(html_noself, n)
        checks[f"empty own_name: {n} falls back to the opt-in list"] = (
            '<span class="muted">—</span>' in r
            and 'action="/api/autoupdate"' in r)

    # ── /api/autoupdate self-guard helper ─────────────────────────────
    cfg = _config(True)
    hc = web_ui.create_handler(cfg, FakeChecker("docksentry"), None, FakeStore())
    hi = hc.__new__(hc)
    checks["_is_own_container: own name -> True"] = hi._is_own_container("docksentry")
    checks["_is_own_container: other name -> False"] = not hi._is_own_container("nginx")
    hc0 = web_ui.create_handler(cfg, FakeChecker(""), None, FakeStore())
    hi0 = hc0.__new__(hc0)
    checks["_is_own_container: empty own name -> False for everything"] = (
        not hi0._is_own_container("docksentry") and not hi0._is_own_container(""))

    # ── Legend: capitalised, translated, emoji-free (#46) ─────────────
    checks["legend capitalises 'auto'"] = ">Auto<" in html or "> Auto<" in html
    checks["legend keeps the lowercase 'auto' in the table header"] = (
        "<th>auto</th>" in html)
    checks["legend has no hardcoded English 'major-confirm'"] = (
        "Major-confirm" in html)
    checks["_legend_word does not mangle 'Auto-Update'"] = (
        web_ui._legend_word("Auto-Update") == "Auto-Update")
    checks["_legend_word strips the emoji and capitalises"] = (
        web_ui._legend_word("🟥 stop") == "Stop")
    checks["_strip_emoji leaves the text alone"] = (
        web_ui._strip_emoji("🔁 Neustart") == "Neustart")
    checks["stop confirm modal is emoji-free"] = (
        'data-confirm-title="Stop"' in html and 'data-confirm-label="Stop"' in html)
    checks["stop button tooltip is a full sentence"] = (
        "it stays offline until someone starts it again" in html)

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
