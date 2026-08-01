#!/usr/bin/env python3
"""Web UI render test for container repo / changelog links (#52, @LeeNX).

Issue #52 asked for two things: a `docksentry.link` label override, and
"possible shows these URLs in the webUI". The second half is what turns a
stored string into an `<a href>` — and that is the moment a value nobody
ever validated becomes script execution. There is no CSP header, the UI
is same-origin with every `/api/*` POST, and `html.escape()` does not
touch a URL *scheme*: `javascript:alert(1)` survives escaping intact and
fires on click.

So this file tests the rendering and the gate around it together:

  - a `javascript:` value sitting in container_links.json produces NO
    href, neither in the status table nor on the detail page (defence in
    depth — set_link validates on the way in, but old backups and
    hand-edited files predate that)
  - a legitimate link produces exactly one anchor, with target="_blank"
    AND rel="noopener noreferrer"
  - the resolution order label → stored → OCI source → OCI url →
    registry guess, and the origin wording that goes with it
  - a `docksentry.link` label disables the /api/link form (field AND
    button) and marks it 🏷 — otherwise the user saves a URL that lands
    in the store while the label keeps winning everywhere
  - /api/backup_import filters `links` through is_safe_link and reports
    what it dropped instead of swallowing it

Same shape as scripts/test_web_selfupdate_row.py: the handler class is
built via create_handler() and instantiated without a socket (__new__).
No Docker, no network, no sockets — `subprocess` is replaced in both
web_ui and telegram_bot so the real resolver chain runs against fake
inspect output.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui                       # noqa: E402
import telegram_bot                 # noqa: E402
import link_resolver
from link_resolver import LinkResolver                # noqa: E402
import container_backend            # noqa: E402
from container_store import ContainerStore   # noqa: E402

MISSING = "/nonexistent/docksentry-test"

EVIL = "javascript:alert(document.cookie)"
GOOD = "https://github.com/amayer1983/docksentry/releases"
LABEL_URL = "https://git.example.com/me/app/-/releases"
OCI_SOURCE = "https://github.com/upstream/app"
OCI_URL = "https://app.example.com"


# ── Fakes ─────────────────────────────────────────────────────────────

class FakeStore:
    """Only what _page_status / _page_container actually touch."""

    def __init__(self, links=None, notes=None):
        self._links = dict(links or {})
        self._notes = dict(notes or {})

    # status table
    def get_pinned(self):
        return []

    def get_autoupdate(self):
        return []

    def get_ask_before_major(self):
        return []

    def get_groups(self):
        return {}

    def get_notes(self):
        return dict(self._notes)

    def get_pending_major(self):
        return {}

    def get_links(self):
        return dict(self._links)

    def is_protect_stop(self, name):
        return False

    # detail page
    def is_pinned(self, name):
        return False

    def is_auto(self, name):
        return False

    def is_ask_before_major(self, name):
        return False

    def is_trust_running(self, name):
        return False

    def get_cooldown(self, name):
        return 0

    def get_update_window(self, name):
        return None

    def get_note(self, name):
        return self._notes.get(name, "")

    def get_link(self, name):
        return self._links.get(name, "")

    def get_group_for_container(self, name):
        return None, None


class FakeChecker:
    def __init__(self, own_name="docksentry", labels=None):
        self._own = own_name
        self._labels = dict(labels or {})

    def _own_container_name(self):
        return self._own

    def get_container_labels(self, name):
        return dict(self._labels)

    def get_disk_usage(self):
        return 42.0, 100 * 1024 ** 3, 200 * 1024 ** 3


class FakeBot:
    """A stand-in Telegram bot, present only so the Web UI takes its
    "real deployment" branch.

    Link resolution no longer lives on the bot: since #52 was extracted
    to `link_resolver.LinkResolver`, the Web UI builds its own resolver
    (`LinkResolver(store, config)`) and never reaches into the bot. So
    this fake needs no resolver methods — just to exist and carry a
    store, which is what guarantees the Web UI and the Telegram
    notifications resolve a link the same way (same module, same code).
    """

    def __init__(self, store):
        self.store = store
        self.config = None


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def fake_subprocess(inspect_obj, labels):
    """A `subprocess` stand-in that answers the docker calls both
    _page_container and LinkResolver.container_source_url make."""
    def run(cmd, **kw):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return FakeProc("123456789")
        if cmd[:2] == ["docker", "inspect"]:
            if "--format" in cmd:
                fmt = cmd[cmd.index("--format") + 1]
                m = re.search(r'"([^"]+)"', fmt)
                if m:
                    return FakeProc(labels.get(m.group(1), "<no value>"))
                if ".Config.Labels" in fmt:
                    return FakeProc(json.dumps(labels))
                return FakeProc("")
            return FakeProc(json.dumps([inspect_obj]))
        return FakeProc("", 1)

    return types.SimpleNamespace(
        run=run,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
    )


def _config():
    return types.SimpleNamespace(
        language="en",
        auto_selfupdate=False,
        ui_mode="advanced",
        debug=False,
        disk_warn_percent=85,
        pending_file=MISSING,
        history_file=MISSING,
        maintenance_file=MISSING,
    )


def c(name, image="nginx:latest", labels=None):
    return {"name": name, "image": image, "health": "healthy",
            "labels": labels or {}, "version": "", "short_id": "abc123456789"}


# ── Render helpers ────────────────────────────────────────────────────

def render_status(containers, links=None):
    cfg = _config()
    store = FakeStore(links=links)
    handler_cls = web_ui.create_handler(cfg, FakeChecker(), FakeBot(store), store)
    h = handler_cls.__new__(handler_cls)
    h.path = "/"
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._get_containers = lambda: list(containers)
    h._page_status()
    return out.get("html", "")


def row_link_of(container, links=None):
    """Call `_row_link` directly, bypassing the status table.

    Lets a check assert what the resolver produced even when the table
    deliberately declines to render it.
    """
    cfg = _config()
    store = FakeStore(links=links)
    handler_cls = web_ui.create_handler(cfg, FakeChecker(), FakeBot(store), store)
    h = handler_cls.__new__(handler_cls)
    return h._row_link(container, store.get_links())


def render_detail(name="nginx", image="nginx:latest", labels=None,
                  links=None, path=None):
    labels = dict(labels or {})
    cfg = _config()
    store = FakeStore(links=links)
    inspect_obj = {
        "Name": "/" + name,
        "Config": {"Image": image, "Labels": labels},
        "State": {"Status": "running", "Health": {"Status": "healthy"},
                  "StartedAt": "2026-07-29T10:00:00.000000000Z"},
        "Created": "2026-07-01T09:00:00.000000000Z",
    }
    fake_sp = fake_subprocess(inspect_obj, labels)
    # The OCI-label lookup (`container_source_url`) now lives in
    # link_resolver, so its `subprocess` module is what the detail page's
    # resolver reaches for — patch it alongside web_ui's (and telegram_bot's,
    # harmless) so the real resolver runs against fake inspect output.
    real_web = web_ui.subprocess
    real_bot = telegram_bot.subprocess
    real_lr = link_resolver.subprocess
    # The detail page's inspect / image-inspect reads now go through the
    # container backend, whose only subprocess window is
    # container_backend.subprocess — patch it too so the fake serves them.
    real_cb = container_backend.subprocess
    web_ui.subprocess = fake_sp
    telegram_bot.subprocess = fake_sp
    link_resolver.subprocess = fake_sp
    container_backend.subprocess = fake_sp
    try:
        checker = FakeChecker(own_name="docksentry", labels=labels)
        handler_cls = web_ui.create_handler(cfg, checker, FakeBot(store), store)
        h = handler_cls.__new__(handler_cls)
        h.path = path or f"/container/{name}"
        out = {}
        h._send_html = lambda html, status=200: out.update(html=html)
        h._page_container(name)
        return out.get("html", "")
    finally:
        web_ui.subprocess = real_web
        telegram_bot.subprocess = real_bot
        link_resolver.subprocess = real_lr
        container_backend.subprocess = real_cb


def link_section(html):
    """The Repo/changelog block of the container settings tab."""
    i = html.find('action="/api/link"')
    if i < 0:
        return ""
    return html[max(0, i - 900):html.find("</form>", i) + 7]


def restore(bundle, tmpdir):
    """Drive POST /api/backup_import against a real ContainerStore."""
    cfg = _config()
    for attr in ("pinned_file", "autoupdate_file", "update_windows_file",
                 "ask_before_major_file", "trust_running_file", "cooldown_file",
                 "protect_stop_file", "major_pending_file", "groups_file",
                 "notes_file", "links_file"):
        setattr(cfg, attr, os.path.join(tmpdir, attr + ".json"))
    store = ContainerStore(cfg)
    handler_cls = web_ui.create_handler(cfg, FakeChecker(), None, store)
    h = handler_cls.__new__(handler_cls)
    body = json.dumps(bundle).encode("utf-8")
    h.path = "/api/backup_import"
    h.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "Host": "localhost:8080",
        "Origin": "http://localhost:8080",
    }
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda *a, **k: None
    h.do_POST()
    return json.loads(h.wfile.getvalue().decode()), store


# ── Checks ────────────────────────────────────────────────────────────

def main():
    checks = {}
    # ── the Web UI must rewrite repo links exactly like the bot (#52) ────
    # The Web UI keeps its OWN copy of the link priority chain, to avoid one
    # `docker inspect` per table row. That copy silently skipped the
    # releases-page rewrite, so the same container linked to
    # /releases/latest in Telegram and to the repo front page in the browser.
    # @LeeNX spotted it on Docksentry's own row. This pins the two together.
    import re as _re                                        # noqa: E402
    import inspect as _inspect                              # noqa: E402
    _src = _inspect.getsource(web_ui.create_handler)
    _row_link_src = _src[_src.find("def _row_link"):]
    _row_link_src = _row_link_src[:_row_link_src.find("def _link_anchor")]
    checks["web UI applies prefer_release_url to auto-detected links"] = (
        "prefer_release_url" in _row_link_src)
    # …and only to the auto-detected ones: an explicit label or /setlink is
    # the user's decision and must come back untouched.
    _before_oci = _row_link_src[:_row_link_src.find("org.opencontainers.image.source")]
    checks["web UI leaves an explicit link alone"] = (
        "prefer_release_url" not in _before_oci)


    # ── 1. A javascript: value in the store never becomes an href ──────
    # This is the whole reason link rendering was held back until the
    # validator existed.
    ev_status = render_status([c("nginx")], links={"nginx": EVIL})
    checks["store javascript:  -> no href in the status table"] = (
        'href="javascript:' not in ev_status)
    checks["store javascript:  -> no 🔗 anchor at all in the table"] = (
        EVIL not in ev_status)

    ev_detail = render_detail("nginx", links={"nginx": EVIL})
    checks["store javascript:  -> no href on the detail page"] = (
        'href="javascript:' not in ev_detail)
    checks["store javascript:  -> no repo row on the detail page"] = (
        ">🔗 <a" not in ev_detail)
    # It may still show up as the *value* of the (escaped) input field —
    # that is text, not a link, and the user has to be able to see and
    # correct what they stored.
    checks["store javascript:  -> still visible as form text"] = (
        f'value="{EVIL}"' in ev_detail)

    # Other schemes the validator rejects must not slip through either.
    # Empty image so the registry heuristic can't supply a *different*
    # (legitimate) link and mask the result — this isolates the store
    # value, which is the thing under test.
    for bad in ("data:text/html;base64,PHNjcmlwdD4=", "vbscript:msgbox",
                "//evil.example/x", "https://exa mple.com",
                'https://x.example/"onclick=alert(1)'):
        h_bad = render_status([c("nginx", image="")], links={"nginx": bad})
        checks[f"store {bad[:26]!r} -> no anchor"] = (
            'target="_blank"' not in _name_cell(h_bad, "nginx"))

    # A rejected value must fall through to the next source rather than
    # blank the row out. Uses an OCI label as that next source: the
    # registry guess is deliberately not shown in the table (see below),
    # so it can't demonstrate fall-through here.
    fall = _name_cell(
        render_status([c("nginx", labels={
            "org.opencontainers.image.source": OCI_SOURCE})],
            links={"nginx": EVIL}), "nginx")
    # The OCI link is auto-detected, so it arrives rewritten to the
    # releases page — same as the bot does. What this asserts is the
    # FALLTHROUGH, not the exact string.
    checks["rejected store value falls through to the OCI label"] = (
        f'href="{LinkResolver.prefer_release_url(OCI_SOURCE)}"' in fall)

    # ── 2. A valid link renders exactly one safe anchor ────────────────
    ok_status = render_status([c("nginx")], links={"nginx": GOOD})
    cell = _name_cell(ok_status, "nginx")
    checks["valid link -> href in the status table"] = f'href="{GOOD}"' in cell
    checks["valid link -> target=_blank"] = 'target="_blank"' in cell
    checks["valid link -> rel=noopener noreferrer"] = (
        'rel="noopener noreferrer"' in cell)
    checks["valid link -> 🔗 as the visible marker"] = ">🔗</a>" in cell
    checks["valid link -> exactly one anchor in the row"] = (
        cell.count('rel="noopener noreferrer"') == 1)
    checks["valid link -> tooltip names the origin"] = (
        "saved in Docksentry" in cell)

    ok_detail = render_detail("nginx", links={"nginx": GOOD})
    checks["valid link -> href on the detail page"] = f'href="{GOOD}"' in ok_detail
    checks["detail anchor carries rel=noopener noreferrer"] = (
        'rel="noopener noreferrer"' in ok_detail)
    checks["detail overview row shows the URL as text"] = (
        f">{GOOD}</a>" in ok_detail)

    # No link anywhere -> no anchor, no empty row. (A bare image name
    # would hit the registry guess, so use a digest-ish ref the
    # heuristic still maps — check the *absence* case via an empty
    # image instead.)
    none_status = render_status([c("nginx", image="")], links={})
    checks["no link at all -> no anchor in the row"] = (
        'rel="noopener noreferrer"' not in _name_cell(none_status, "nginx"))

    # ── 3. Resolution order + origin wording ───────────────────────────
    full_labels = {
        "docksentry.link": LABEL_URL,
        "org.opencontainers.image.source": OCI_SOURCE,
        "org.opencontainers.image.url": OCI_URL,
    }
    row_all = _name_cell(
        render_status([c("nginx", labels=full_labels)], links={"nginx": GOOD}),
        "nginx")
    checks["order: docksentry.link beats store + OCI"] = (
        f'href="{LABEL_URL}"' in row_all)
    checks["order: label origin named in the tooltip"] = (
        "docksentry.link container label" in row_all)

    row_store = _name_cell(
        render_status([c("nginx", labels={
            "org.opencontainers.image.source": OCI_SOURCE})],
            links={"nginx": GOOD}), "nginx")
    checks["order: store beats the OCI source label"] = f'href="{GOOD}"' in row_store

    row_src = _name_cell(
        render_status([c("nginx", labels={
            "org.opencontainers.image.source": OCI_SOURCE,
            "org.opencontainers.image.url": OCI_URL})]), "nginx")
    checks["order: OCI source beats OCI url"] = (
        f'href="{LinkResolver.prefer_release_url(OCI_SOURCE)}"' in row_src)
    checks["order: OCI source origin named"] = (
        "org.opencontainers.image.source" in row_src)

    row_url = _name_cell(
        render_status([c("nginx", labels={
            "org.opencontainers.image.url": OCI_URL})]), "nginx")
    checks["order: OCI url used as fallback"] = f'href="{OCI_URL}"' in row_url

    # Registry heuristic: resolved, but NOT shown in the status table.
    # It's our own guess at an overview page derived from the image
    # name — a registry landing page, not the changelog #52 asked for —
    # and it applies to most containers, so showing it would put an icon
    # on nearly every row leading somewhere nobody asked to go. The
    # table is also where #37 and #46 asked us to keep the noise down.
    row_reg = _name_cell(
        render_status([c("app", image="ghcr.io/owner/app:latest")]), "app")
    checks["registry guess is not linked in the status table"] = (
        'target="_blank"' not in row_reg)
    # …but only the table suppresses it. The resolver still produces it,
    # which is what Telegram and the container page keep using — this is
    # a display decision, not a change to the chain.
    _reg_url, _reg_kind = row_link_of(c("app", image="ghcr.io/owner/app:latest"))
    checks["registry guess still resolves for other surfaces"] = (
        _reg_url == "https://github.com/owner/app/pkgs/container/app"
        and _reg_kind == "registry")

    # A malformed label falls through to the next source instead of
    # breaking the row — a typo in a compose file must not cost the user
    # their stored link.
    row_badlabel = _name_cell(
        render_status([c("nginx", labels={"docksentry.link": EVIL})],
                      links={"nginx": GOOD}), "nginx")
    checks["unsafe docksentry.link falls through to the stored link"] = (
        f'href="{GOOD}"' in row_badlabel and EVIL not in row_badlabel)

    # ── 4. The form is disabled when a label wins ──────────────────────
    lab_detail = render_detail(
        "nginx", labels={"docksentry.link": LABEL_URL}, links={"nginx": GOOD})
    sec = link_section(lab_detail)
    checks["label set -> input disabled"] = (
        '<input type="url" name="url"' in sec and " disabled" in sec)
    checks["label set -> save button disabled too"] = (
        sec.count(" disabled") >= 2)
    checks["label set -> 🏷 marker on the section"] = (
        "🏷" in lab_detail and 'class="label-mark"' in lab_detail)
    checks["label set -> 🏷 uses the existing web_label_authoritative text"] = (
        "Controlled by a docksentry.* label in the compose file" in lab_detail)
    checks["label set -> field shows the EFFECTIVE (label) URL"] = (
        f'value="{LABEL_URL}"' in sec)
    checks["label set -> the label URL is what gets linked"] = (
        f'href="{LABEL_URL}"' in lab_detail)

    free_detail = render_detail("nginx", links={"nginx": GOOD})
    free_sec = link_section(free_detail)
    checks["no label -> input usable"] = (
        '<input type="url" name="url"' in free_sec
        and " disabled" not in free_sec)
    checks["no label -> no 🏷 on the link section"] = "🏷" not in free_sec
    checks["no label -> field shows the stored URL"] = (
        f'value="{GOOD}"' in free_sec)

    # An unsafe label must NOT lock the form: it is ignored by the
    # resolver, so the stored value is what actually applies and the
    # user has to stay able to edit it.
    badlab_detail = render_detail("nginx", labels={"docksentry.link": EVIL},
                                  links={"nginx": GOOD})
    checks["unsafe label -> form stays usable"] = (
        " disabled" not in link_section(badlab_detail))

    # ── 5. /api/link tells the user what happened ──────────────────────
    for kind, needle in (("saved", "Link saved."),
                         ("cleared", "Link removed"),
                         ("rejected", "Link rejected")):
        html_msg = render_detail("nginx", path=f"/container/nginx?link={kind}")
        checks[f"/api/link feedback: ?link={kind} renders a notice"] = (
            needle in html_msg)
    checks["/api/link feedback: no notice without the query param"] = (
        "Link rejected" not in free_detail and "Link saved." not in free_detail)

    # ── 6. Backup restore filters the links section ────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        resp, store = restore({"links": {
            "good": GOOD,
            "evil": EVIL,
            "data": "data:text/html;base64,PHNjcmlwdD4=",
            "relative": "/api/update",
            "padded": f"  {OCI_SOURCE}  ",
            "padded_evil": f"  {EVIL}  ",
            "nested": {"url": GOOD},
        }}, tmp)
        stored = store.get_links()
        checks["restore: valid entry kept"] = stored.get("good") == GOOD
        checks["restore: javascript: entry dropped"] = "evil" not in stored
        checks["restore: data: entry dropped"] = "data" not in stored
        checks["restore: relative path dropped"] = "relative" not in stored
        checks["restore: non-string value dropped"] = "nested" not in stored
        # Same normalisation the live write path (set_link) applies:
        # surrounding whitespace is stripped, then validated. Stripping
        # must not launder a bad scheme — " javascript:…" still goes.
        checks["restore: padded valid URL kept, trimmed"] = (
            stored.get("padded") == OCI_SOURCE)
        checks["restore: padded javascript: still dropped"] = (
            "padded_evil" not in stored)
        checks["restore: exactly two entries survive"] = len(stored) == 2
        checks["restore: drop count reported"] = resp.get("links_dropped") == 5
        checks["restore: drop count visible in the toast text"] = any(
            "5 unsafe dropped" in s for s in resp.get("restored", []))
        checks["restore: drop also listed under errors"] = any(
            s.startswith("links:") for s in resp.get("errors", []))

    with tempfile.TemporaryDirectory() as tmp:
        resp2, store2 = restore({"links": {"a": GOOD, "b": OCI_SOURCE}}, tmp)
        checks["restore: clean bundle keeps everything"] = (
            store2.get_links() == {"a": GOOD, "b": OCI_SOURCE})
        checks["restore: clean bundle reports plain 'links'"] = (
            "links" in resp2.get("restored", []))
        checks["restore: clean bundle drops nothing"] = (
            resp2.get("links_dropped") == 0)

    # A restored-then-rendered unsafe value: belt and braces. Even if a
    # future bundle path forgets to filter, the render gate holds.
    checks["render gate holds independently of the restore filter"] = (
        'href="javascript:' not in render_status([c("evil")],
                                                 links={"evil": EVIL}))

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


def _name_cell(html, name):
    """The name <td> of the row for `name` — where the 🔗 lives."""
    marker = f'/container/{name}"'
    for chunk in html.split("<tr>"):
        if marker in chunk:
            start = chunk.find(marker)
            end = chunk.find("</td>", start)
            return chunk[start:end if end > 0 else len(chunk)]
    return ""


if __name__ == "__main__":
    sys.exit(main())
